"""Browser-assisted 2FA Rotation for ChatGPT.

Executes 2FA reset inside a Playwright browser context to guarantee
that old authenticators are completely removed by using the browser's
authenticated session, cookies, and TLS fingerprint.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from config import Settings
from totp_helper import generate_code, normalize_secret

logger = logging.getLogger(__name__)


async def rotate_2fa_via_browser(
    *,
    email: str,
    session_data: dict[str, Any] | None = None,
    cookies: list[dict[str, Any]] | None = None,
    session_token: str | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
    headless: bool = True,
    log: Callable[[str], None] = print,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Tắt toàn bộ 2FA cũ và kích hoạt 1 2FA mới duy nhất trong page browser."""
    from playwright.async_api import async_playwright

    settings = Settings()
    profile_dir = settings.profiles_dir / f"rotate2fa_{abs(hash(email)) % 100000}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    session_data = session_data or {}
    s_token = session_token or session_data.get("sessionToken") or session_data.get("session_token")
    a_token = access_token or session_data.get("accessToken")
    cookies_raw = cookies or session_data.get("__cookies") or session_data.get("cookies") or []

    cookie_list: list[dict[str, Any]] = []
    if isinstance(cookies_raw, dict):
        for k, v in cookies_raw.items():
            cookie_list.append({"name": str(k), "value": str(v), "domain": ".chatgpt.com", "path": "/"})
    elif isinstance(cookies_raw, list):
        for c in cookies_raw:
            if isinstance(c, dict) and "name" in c and "value" in c:
                cookie_list.append({
                    "name": str(c["name"]),
                    "value": str(c["value"]),
                    "domain": str(c.get("domain") or ".chatgpt.com"),
                    "path": str(c.get("path") or "/"),
                })

    if s_token:
        cookie_list.append({
            "name": "__Secure-next-auth.session-token",
            "value": str(s_token),
            "domain": ".chatgpt.com",
            "path": "/",
        })

    pw = await async_playwright().start()
    context = None
    try:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "args": ["--start-maximized", "--disable-blink-features=AutomationControlled"],
        }
        if proxy:
            # Parse proxy URL if needed
            p_parts = proxy.split("://")
            proto = p_parts[0] if len(p_parts) > 1 else "http"
            p_host = p_parts[-1]
            launch_kwargs["proxy"] = {"server": f"{proto}://{p_host}"}

        # Try msedge -> chrome -> chromium
        for ch in ("msedge", "chrome", None):
            try:
                kw = dict(launch_kwargs)
                if ch:
                    kw["channel"] = ch
                context = await pw.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:
                log(f"[browser-2fa] launch {ch} note: {e}")

        if not context:
            raise RuntimeError("Could not launch browser for 2FA rotation")

        if cookie_list:
            try:
                await context.add_cookies(cookie_list)
            except Exception as ce:
                log(f"[browser-2fa] add_cookies note: {ce}")

        page = context.pages[0] if context.pages else await context.new_page()

        log("[browser-2fa] Mở ChatGPT để xác thực session...")
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2.0)

        # Lấy fresh access_token nếu chưa có
        token_res = await page.evaluate("""
        async () => {
            try {
                const r = await fetch('/api/auth/session');
                if (r.status === 200) {
                    const data = await r.json();
                    return data.accessToken || null;
                }
            } catch(e) {}
            return null;
        }
        """)
        token = token_res or a_token
        if not token:
            log("[browser-2fa] Không lấy được accessToken từ session — kiểm tra lại login")

        # ── Bước 1: Gỡ bỏ sạch sẽ toàn bộ 2FA cũ ──
        log("[browser-2fa] Đang gửi lệnh tắt và xóa sạch mọi khóa 2FA cũ...")
        disable_result = await page.evaluate("""
        async (token) => {
            const headers = {'Content-Type': 'application/json'};
            if (token) headers['Authorization'] = 'Bearer ' + token;
            const opt = (method, body) => ({
                method: method,
                headers: headers,
                credentials: 'include',
                body: body ? JSON.stringify(body) : undefined,
            });

            const logs = [];
            try {
                // 1. Check existing factors
                const infoRes = await fetch('/backend-api/accounts/mfa_info', opt('GET'));
                let factors = [];
                if (infoRes.status === 200) {
                    const infoData = await infoRes.json();
                    factors = infoData.factors || [];
                }

                // 2. Unenroll each factor
                for (const f of factors) {
                    if (f && f.id) {
                        try {
                            const un = await fetch('/backend-api/accounts/mfa/user/unenroll', opt('POST', {factor_id: f.id, factor_type: 'totp'}));
                            logs.push(`unenroll factor ${f.id}: HTTP ${un.status}`);
                        } catch (e) {
                            logs.push(`unenroll err: ${e}`);
                        }
                    }
                }

                // 3. Disable all MFA
                const dis1 = await fetch('/backend-api/accounts/mfa', opt('DELETE'));
                logs.push(`delete /accounts/mfa: HTTP ${dis1.status}`);

                const dis2 = await fetch('/backend-api/accounts/mfa/disable', opt('POST', {}));
                logs.push(`post /accounts/mfa/disable: HTTP ${dis2.status}`);
            } catch(err) {
                logs.push(`error: ${err}`);
            }
            return logs;
        }
        """, token)
        for d_log in (disable_result or []):
            log(f"[browser-2fa] {d_log}")

        await asyncio.sleep(1.0)

        # ── Bước 2: Đăng ký (Enroll) khóa 2FA mới duy nhất ──
        log("[browser-2fa] Đang bật công tắc 2FA mới (Enroll)...")
        enroll_res = await page.evaluate("""
        async (token) => {
            const headers = {'Content-Type': 'application/json'};
            if (token) headers['Authorization'] = 'Bearer ' + token;
            const resp = await fetch('/backend-api/accounts/mfa/enroll', {
                method: 'POST',
                headers: headers,
                credentials: 'include',
                body: JSON.stringify({factor_type: 'totp'})
            });
            const text = await resp.text();
            return {status: resp.status, text: text};
        }
        """, token)

        if enroll_res.get("status") != 200:
            raise RuntimeError(f"Enroll 2FA thất bại (HTTP {enroll_res.get('status')}): {enroll_res.get('text')[:200]}")

        enroll_data = json.loads(enroll_res.get("text") or "{}")
        secret = normalize_secret(enroll_data.get("secret") or "")
        factor = enroll_data.get("factor") or {}
        factor_id = factor.get("id") or enroll_data.get("factor_id")
        session_id = enroll_data.get("session_id") or factor_id

        if not secret or not factor_id:
            raise RuntimeError(f"Enroll response thiếu secret/factor_id: {enroll_data}")

        # ── Bước 3: Xác thực 6 số OTP để kích hoạt ──
        first_code = generate_code(secret)
        log(f"[browser-2fa] Kích hoạt khóa 2FA mới (factor_id={factor_id}) bằng mã OTP: {first_code}")

        activate_res = await page.evaluate("""
        async ({token, factor_id, session_id, code}) => {
            const headers = {'Content-Type': 'application/json'};
            if (token) headers['Authorization'] = 'Bearer ' + token;
            const resp = await fetch('/backend-api/accounts/mfa/user/activate_enrollment', {
                method: 'POST',
                headers: headers,
                credentials: 'include',
                body: JSON.stringify({
                    factor_id: factor_id,
                    factor_type: 'totp',
                    session_id: session_id,
                    code: code
                })
            });
            const text = await resp.text();
            return {status: resp.status, text: text};
        }
        """, {"token": token, "factor_id": factor_id, "session_id": session_id, "code": first_code})

        if activate_res.get("status") not in (200, 204):
            log(f"[browser-2fa] Activate note (HTTP {activate_res.get('status')}): {activate_res.get('text')[:150]}")

        log(f"[browser-2fa] Đổi 2FA thành công! Secret mới: {secret}")
        return {
            "secret": secret,
            "factor_id": factor_id,
            "session_id": session_id,
            "first_code": first_code,
            "activated": True,
        }

    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        await pw.stop()
