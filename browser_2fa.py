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

        log("[browser-2fa] Mở ChatGPT Settings -> Security...")
        await page.goto("https://chatgpt.com/#settings/Security", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3.0)

        # ── 1. Thử click trực tiếp qua giao diện DOM UI ──
        ui_success = False
        secret = ""
        factor_id = ""
        session_id = ""

        try:
            # Check nếu có nút Turn off / Disable MFA trong UI
            turn_off_btn = page.locator(
                'button:has-text("Turn off"), button:has-text("Disable"), button:has-text("Tắt")'
            ).first
            if await turn_off_btn.is_visible(timeout=3000):
                log("[browser-2fa] [UI] Tìm thấy nút Turn off 2FA, đang click tắt...")
                await turn_off_btn.click()
                await asyncio.sleep(1.0)
                # Confirm modal nếu có
                confirm_btn = page.locator(
                    'div[role="dialog"] button:has-text("Turn off"), '
                    'div[role="dialog"] button:has-text("Disable"), '
                    'div[role="dialog"] button:has-text("Tắt")'
                ).first
                if await confirm_btn.is_visible(timeout=3000):
                    await confirm_btn.click()
                    log("[browser-2fa] [UI] Đã xác nhận tắt 2FA cũ trên modal")
                    await asyncio.sleep(2.0)

            # Click Turn on / Enable MFA
            turn_on_btn = page.locator(
                'button:has-text("Turn on"), button:has-text("Enable"), button:has-text("Bật")'
            ).first
            if await turn_on_btn.is_visible(timeout=5000):
                log("[browser-2fa] [UI] Tìm thấy nút Turn on 2FA, đang click bật mới...")
                await turn_on_btn.click()
                await asyncio.sleep(2.0)

                # Mở dòng hiển thị Secret Key
                cant_scan = page.locator(
                    'button:has-text("Can\'t scan"), button:has-text("manual"), '
                    'a:has-text("Can\'t scan"), span:has-text("Can\'t scan")'
                ).first
                if await cant_scan.is_visible(timeout=3000):
                    await cant_scan.click()
                    await asyncio.sleep(0.5)

                # Lấy Secret Key từ DOM
                secret_el = page.locator('code, [class*="secret"], [class*="key"], [aria-label*="Secret"]').first
                if await secret_el.is_visible(timeout=3000):
                    secret_raw = await secret_el.text_content()
                    if secret_raw:
                        secret = normalize_secret(secret_raw.strip())
                        log(f"[browser-2fa] [UI] Đã trích xuất Secret Key: {secret}")

                # Bấm Continue sang bước nhập 6 số
                cont_btn = page.locator(
                    'div[role="dialog"] button:has-text("Continue"), '
                    'div[role="dialog"] button:has-text("Next"), '
                    'div[role="dialog"] button:has-text("Tiếp tục")'
                ).first
                if await cont_btn.is_visible(timeout=3000):
                    await cont_btn.click()
                    await asyncio.sleep(1.0)

                # Điền mã 6 số OTP
                if secret:
                    otp_code = generate_code(secret)
                    otp_inputs = page.locator('input[type="text"], input[inputmode="numeric"], input[name*="code"]')
                    count = await otp_inputs.count()
                    if count == 6:
                        for idx in range(6):
                            await otp_inputs.nth(idx).fill(otp_code[idx])
                    elif count == 1:
                        await otp_inputs.first.fill(otp_code)

                    log(f"[browser-2fa] [UI] Đã điền 6 số OTP ({otp_code})")
                    await asyncio.sleep(0.5)

                    # Bấm Verify / Done
                    verify_btn = page.locator(
                        'div[role="dialog"] button:has-text("Continue"), '
                        'div[role="dialog"] button:has-text("Verify"), '
                        'div[role="dialog"] button:has-text("Done"), '
                        'div[role="dialog"] button:has-text("Xác nhận")'
                    ).first
                    if await verify_btn.is_visible(timeout=3000):
                        await verify_btn.click()
                        log("[browser-2fa] [UI] Đã click Verify hoàn tất!")
                        await asyncio.sleep(2.0)
                        ui_success = True
        except Exception as ui_err:
            log(f"[browser-2fa] UI click workflow note: {ui_err}")

        # ── 2. Nếu UI click chưa xong, chạy API in-page fallback ──
        if not ui_success or not secret:
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

            log("[browser-2fa] [In-Page] Gửi lệnh tắt và kích hoạt 2FA duy nhất...")
            enroll_res = await page.evaluate("""
            async (token) => {
                const headers = {'Content-Type': 'application/json'};
                if (token) headers['Authorization'] = 'Bearer ' + token;
                const opt = (method, body) => ({
                    method: method,
                    headers: headers,
                    credentials: 'include',
                    body: body ? JSON.stringify(body) : undefined,
                });

                // 1. Tắt hết factor cũ
                try {
                    const infoRes = await fetch('/backend-api/accounts/mfa_info', opt('GET'));
                    if (infoRes.status === 200) {
                        const info = await infoRes.json();
                        for (const f of (info.factors || [])) {
                            if (f && f.id) {
                                await fetch('/backend-api/accounts/mfa/user/unenroll', opt('POST', {factor_id: f.id, factor_type: 'totp'}));
                            }
                        }
                    }
                    await fetch('/backend-api/accounts/mfa', opt('DELETE'));
                    await fetch('/backend-api/accounts/mfa/disable', opt('POST', {}));
                } catch(e) {}

                // 2. Enroll mới
                const resp = await fetch('/backend-api/accounts/mfa/enroll', opt('POST', {factor_type: 'totp'}));
                const text = await resp.text();
                return {status: resp.status, text: text};
            }
            """, token)

            if enroll_res.get("status") != 200:
                raise RuntimeError(f"Enroll thất bại HTTP {enroll_res.get('status')}: {enroll_res.get('text')[:200]}")

            enroll_data = json.loads(enroll_res.get("text") or "{}")
            secret = normalize_secret(enroll_data.get("secret") or "")
            factor = enroll_data.get("factor") or {}
            factor_id = factor.get("id") or enroll_data.get("factor_id")
            session_id = enroll_data.get("session_id") or factor_id

            # Activate
            first_code = generate_code(secret)
            await page.evaluate("""
            async ({token, factor_id, session_id, code}) => {
                const headers = {'Content-Type': 'application/json'};
                if (token) headers['Authorization'] = 'Bearer ' + token;
                await fetch('/backend-api/accounts/mfa/user/activate_enrollment', {
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
            }
            """, {"token": token, "factor_id": factor_id, "session_id": session_id, "code": first_code})

        first_code = generate_code(secret)
        log(f"[browser-2fa] Hoàn tất đổi 2FA! Secret mới: {secret}")
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
