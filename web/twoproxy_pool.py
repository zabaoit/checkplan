"""TwoProxyPool — Quản lý và xoay API Key 2Proxy.vn cho Reg và Offer 0đ/Check Plan.

Tài liệu API: https://app.2proxy.vn/document-api.html
Endpoint xoay: https://app.2proxy.vn/api/proxy.php?key={KEY}&nhamang=all

Tính năng:
- Độc lập với proxy pool thường; khi bật 2Proxy, ưu tiên lấy IP từ key xoay.
- Mỗi job song song (Reg, Offer 0đ / Chpwd) giữ 1 key riêng biệt.
- Tự động cache proxy theo thời gian Live IP (mặc định 120s = 2 phút) để tránh
  gọi API đổi IP liên tục khi chưa hết hạn.
- Hết hạn hoặc job mới cần IP mới -> gọi 2Proxy API lấy IP xoay mới.
- Hết key rảnh -> job chờ key hoặc báo lỗi thay vì chạy direct.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any
import urllib.request
import urllib.parse
import urllib.error

try:
    from .proxy_format import materialize_proxy, mask_proxy
except ImportError:
    from proxy_format import materialize_proxy, mask_proxy

_log = logging.getLogger(__name__)

_TWOPROXY_API_URL = "https://api.proxyxoay.org/api/key_xoay.php"
_TWOPROXY_FALLBACK_URL = "https://app.2proxy.vn/api/key_xoay.php"


def normalize_keys(keys: Any) -> list[str]:
    """Strip + bỏ rỗng + dedupe (giữ thứ tự)."""
    if not keys:
        return []
    if isinstance(keys, str):
        keys = keys.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for item in keys:
        if not isinstance(item, str):
            continue
        val = item.strip()
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(val)
    return out


def mask_key(key: str | None) -> str:
    """Mask key để an toàn khi ghi log (ví dụ: ab12***89)."""
    if not key:
        return ""
    k = str(key).strip()
    if len(k) <= 6:
        return k[:2] + "***"
    return k[:3] + "***" + k[-3:]


class TwoProxyPool:
    """Pool quản lý các API Key 2Proxy.vn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled: bool = False
        self._keys: list[str] = []
        self._live_seconds: int = 120
        self._cursor: int = 0
        # key -> số job đang lease key đó
        self._leases: dict[str, int] = {}
        # key -> {"proxy_url": str, "proxy_raw": str, "ip": str, "obtained_at": float, "expires_at": float}
        self._cache: dict[str, dict[str, Any]] = {}
        self._async_cond: asyncio.Condition | None = None

    def _get_cond(self) -> asyncio.Condition:
        if self._async_cond is None:
            self._async_cond = asyncio.Condition()
        return self._async_cond

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    @property
    def live_seconds(self) -> int:
        return self._live_seconds

    def is_active(self) -> bool:
        """True nếu 2Proxy đang bật và có ít nhất 1 key cấu hình."""
        return self._enabled and len(self._keys) > 0

    def configure(
        self,
        *,
        enabled: bool | None = None,
        keys: list[str] | None = None,
        live_seconds: int | None = None,
    ) -> None:
        """Cấu hình lại trạng thái 2Proxy."""
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            if keys is not None:
                normalized = normalize_keys(keys)
                present = set(normalized)
                self._keys = normalized
                self._leases = {k: c for k, c in self._leases.items() if k in present}
                self._cache = {k: v for k, v in self._cache.items() if k in present}
                if self._cursor >= len(self._keys):
                    self._cursor = 0
            if live_seconds is not None:
                self._live_seconds = max(0, min(600, int(live_seconds)))

    def status(self) -> dict[str, Any]:
        """Thông tin trạng thái runtime của 2Proxy."""
        with self._lock:
            now = time.monotonic()
            active_ips: dict[str, str] = {}
            for k, info in self._cache.items():
                if info.get("expires_at", 0) > now:
                    active_ips[mask_key(k)] = info.get("ip", "")
            return {
                "enabled": self._enabled,
                "key_count": len(self._keys),
                "keys_masked": [mask_key(k) for k in self._keys],
                "leased_count": sum(self._leases.values()),
                "live_seconds": self._live_seconds,
                "active_ips": active_ips,
            }

    async def fetch_proxy_from_api(
        self, key: str, live_seconds: int = 120
    ) -> dict[str, Any]:
        """Gọi API 2Proxy để lấy IP / xoay IP.

        Endpoint: https://api.proxyxoay.org/api/key_xoay.php?key={KEY}&live={LIVE_MINUTES}
        """
        k = key.strip()
        if not k:
            return {"ok": False, "error": "API key trống"}

        # Chuyển seconds sang minutes cho tham số live (1, 2, 3, 4, 5 phút)
        live_min = max(1, min(5, int(round(live_seconds / 60.0)))) if live_seconds > 0 else 1
        params = {"key": k, "live": live_min}

        target_urls = [
            f"{_TWOPROXY_API_URL}?{urllib.parse.urlencode(params)}",
            f"{_TWOPROXY_FALLBACK_URL}?{urllib.parse.urlencode(params)}",
        ]

        last_error = ""

        for target_url in target_urls:
            try:
                try:
                    import httpx
                    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.get(target_url)
                        status_code = resp.status_code
                        text = resp.text
                except ImportError:
                    def _fetch(u=target_url):
                        req = urllib.request.Request(
                            u,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                        )
                        with urllib.request.urlopen(req, timeout=12) as r:
                            return r.status, r.read().decode("utf-8", errors="ignore")
                    status_code, text = await asyncio.to_thread(_fetch)

                if status_code != 200:
                    last_error = f"HTTP {status_code}: {text[:100]}"
                    continue

                try:
                    data = json.loads(text)
                except Exception:
                    raw_txt = text.strip()
                    if ":" in raw_txt:
                        url = materialize_proxy(raw_txt)
                        ip = raw_txt.split(":")[0]
                        return {
                            "ok": True,
                            "proxy_url": url,
                            "proxy_raw": raw_txt,
                            "ip": ip,
                            "comen": "OK",
                        }
                    last_error = f"Phản hồi không phải JSON: {text[:100]}"
                    continue

                # Parse JSON response từ key_xoay.php
                raw_proxy = (
                    data.get("proxyhttp")
                    or data.get("proxy")
                    or data.get("proxysocks5")
                    or data.get("http_proxy")
                    or (data.get("data") and (data["data"].get("proxyhttp") or data["data"].get("proxy")))
                )

                if raw_proxy:
                    try:
                        url = materialize_proxy(str(raw_proxy))
                    except Exception as ex:
                        return {"ok": False, "error": f"Lỗi định dạng proxy '{raw_proxy}': {ex}"}

                    ip = data.get("ip") or str(raw_proxy).split(":")[0]
                    return {
                        "ok": True,
                        "proxy_url": url,
                        "proxy_raw": str(raw_proxy),
                        "ip": str(ip),
                        "comen": "Thành công",
                        "time_die": data.get("time_die"),
                        "next_allowed": data.get("next_allowed_in_seconds"),
                    }

                # Kiểm tra message lỗi
                comen = (
                    data.get("message")
                    or data.get("error")
                    or data.get("comen")
                    or f"Mã lỗi: {data.get('maloi')}"
                )
                return {
                    "ok": False,
                    "error": str(comen),
                    "comen": str(comen),
                    "raw_data": data,
                }

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

        return {"ok": False, "error": last_error or "Không kết nối được tới 2Proxy API"}

    async def test_key(self, key: str, live_seconds: int = 120) -> dict[str, Any]:
        """Test 1 API key 2Proxy và kiểm tra kết nối proxy."""
        res = await self.fetch_proxy_from_api(key, live_seconds=live_seconds)
        if not res["ok"]:
            return {
                "key": key,
                "key_masked": mask_key(key),
                "ok": False,
                "public_ip": None,
                "detail": f"2Proxy API lỗi: {res.get('error', 'Unknown')}",
                "maloi": res.get("maloi"),
            }

        proxy_url = res["proxy_url"]
        reported_ip = res.get("ip")

        # Probe proxy kết nối thực tế
        try:
            import httpx
            timeout = httpx.Timeout(connect=8.0, read=10.0, write=8.0, pool=8.0)
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout) as client:
                r = await client.get("https://chatgpt.com/cdn-cgi/trace")
                if r.status_code < 500:
                    public_ip = reported_ip
                    for line in r.text.splitlines():
                        if line.startswith("ip="):
                            public_ip = line[3:].strip()
                            break
                    return {
                        "key": key,
                        "key_masked": mask_key(key),
                        "ok": True,
                        "public_ip": public_ip,
                        "proxy": res.get("proxy_raw"),
                        "detail": f"Live OK (IP: {public_ip})",
                    }
        except Exception:
            pass

        return {
            "key": key,
            "key_masked": mask_key(key),
            "ok": True,
            "public_ip": reported_ip,
            "proxy": res.get("proxy_raw"),
            "detail": f"Live OK (IP: {reported_ip})",
        }

    async def acquire(
        self, log=None, timeout: float = 45.0
    ) -> tuple[str, str]:
        """Lease 1 key và lấy proxy live (URL, raw_key).

        - Nếu có key rảnh: lease key đó.
        - Nếu tất cả key đang bận (do các job song song khác đang giữ):
          chờ cho tới khi có key được release hoặc hết timeout.
        - Kiểm tra cache: nếu IP của key này còn trong hạn Live IP -> dùng lại.
        - Nếu hết hạn -> gọi 2Proxy API lấy IP mới.
        - Trả về (proxy_url, key).
        """
        if not self._enabled:
            raise RuntimeError("2Proxy chưa được bật trong cài đặt")

        with self._lock:
            if not self._keys:
                raise RuntimeError("Danh sách key 2Proxy đang trống, vui lòng thêm key")

        cond = self._get_cond()
        deadline = time.monotonic() + max(5.0, timeout)
        leased_key: str | None = None

        while leased_key is None:
            with self._lock:
                # Tìm key có ít lease nhất (hoặc lease == 0)
                # Ưu tiên key chưa có job nào giữ (lease == 0)
                available = [k for k in self._keys if self._leases.get(k, 0) == 0]
                if available:
                    # Chọn tuần tự qua round-robin cursor
                    leased_key = available[self._cursor % len(available)]
                    self._cursor += 1
                    self._leases[leased_key] = self._leases.get(leased_key, 0) + 1
                else:
                    # Nếu số job song song vượt quá số key, kiểm tra xem có được phép chia sẻ key không:
                    # Quy tắc: Mỗi job giữ 1 key riêng. Nếu không còn key rảnh, phải chờ.
                    pass

            if leased_key is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"2Proxy: Tất cả {len(self._keys)} key đang bận do các job khác đang chạy, quá thời gian chờ ({timeout:.0f}s)"
                    )
                if log:
                    log(f"[2proxy] Tất cả {len(self._keys)} key đang bận, đang chờ key rảnh ({remaining:.0f}s)...")
                async with cond:
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=min(3.0, remaining))
                    except asyncio.TimeoutError:
                        pass

        # Đã lease được leased_key
        key_masked = mask_key(leased_key)
        now = time.monotonic()

        # Kiểm tra cache
        cached = self._cache.get(leased_key)
        if (
            cached
            and self._live_seconds > 0
            and cached.get("expires_at", 0) > now
            and cached.get("proxy_url")
        ):
            rem_sec = int(cached["expires_at"] - now)
            if log:
                log(f"[2proxy] key {key_masked}: Dùng lại IP live {cached.get('ip')} (còn {rem_sec}s)")
            return cached["proxy_url"], leased_key

        # Cần lấy IP mới từ API
        if log:
            log(f"[2proxy] key {key_masked}: Đang gọi API lấy IP xoay...")

        res = await self.fetch_proxy_from_api(leased_key, live_seconds=self._live_seconds)
        if res["ok"]:
            proxy_url = res["proxy_url"]
            ip = res.get("ip", "")
            expires_at = time.monotonic() + max(5, self._live_seconds)
            with self._lock:
                self._cache[leased_key] = {
                    "proxy_url": proxy_url,
                    "proxy_raw": res.get("proxy_raw"),
                    "ip": ip,
                    "obtained_at": time.monotonic(),
                    "expires_at": expires_at,
                }
            if log:
                log(f"[2proxy] key {key_masked}: Lấy IP mới thành công: {ip} (live {self._live_seconds}s)")
            return proxy_url, leased_key

        # Nếu API trả về lỗi nhưng ta có IP trong cache cũ (chưa quá xa)
        if cached and cached.get("proxy_url"):
            if log:
                log(f"[2proxy] key {key_masked}: API xoay báo '{res.get('error')}', dùng tạm IP cũ {cached.get('ip')}")
            return cached["proxy_url"], leased_key

        # Thất bại hoàn toàn -> nhả lease và báo lỗi
        self.release(leased_key)
        err_msg = res.get("error") or "Không lấy được proxy từ 2Proxy"
        raise RuntimeError(f"2Proxy key {key_masked} lỗi: {err_msg}")

    def release(self, key: str | None, *, clear_cache: bool = True) -> None:
        """Trả lease key khi job kết thúc.
        
        Nếu clear_cache=True (mặc định), xóa cached IP của key này để job sau
        phải gọi API lấy IP mới thay vì dùng lại IP cũ còn live.
        """
        if not key:
            return
        with self._lock:
            if key in self._leases:
                self._leases[key] = max(0, self._leases[key] - 1)
                if self._leases[key] == 0:
                    self._leases.pop(key, None)
            if clear_cache:
                self._cache.pop(key, None)

        if self._async_cond is not None:
            try:
                # Notify condition
                asyncio.create_task(self._notify_cond())
            except Exception:
                pass

    async def _notify_cond(self) -> None:
        if self._async_cond is not None:
            async with self._async_cond:
                self._async_cond.notify_all()


_twoproxy_pool: TwoProxyPool | None = None


def get_twoproxy_pool() -> TwoProxyPool:
    """Return the singleton instance of TwoProxyPool."""
    global _twoproxy_pool
    if _twoproxy_pool is None:
        _twoproxy_pool = TwoProxyPool()
    return _twoproxy_pool
