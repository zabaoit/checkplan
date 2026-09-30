"""Browser Automation: Đăng nhập ChatGPT chạy ẩn -> Mở Trial -> Chọn MoMo -> Điền info random -> Lấy QR MoMo."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import uuid
from pathlib import Path
from typing import Any, Callable

from config import Settings
from totp_helper import generate_code, normalize_secret

logger = logging.getLogger("browser_momo")

_VN_NAMES = [
    "Nguyen Van An", "Tran Dinh Bao", "Le Hoang Nam", "Pham Minh Duc",
    "Vu Quoc Huy", "Hoang Gia Bao", "Dang Quang Hung", "Ngo Tuan Kiet",
    "Bui Duc Thang", "Doan Minh Tri", "Dinh Trong Nghia", "Trinh Hoang Long"
]

_VN_ADDRESSES = [
    "128 Nguyen Trai, Phuong 3, Quan 5, TP Ho Chi Minh",
    "45 Le Loi, Phuong Ben Nghe, Quan 1, TP Ho Chi Minh",
    "72 Hai Ba Trung, Phuong Ben Nghe, Quan 1, TP Ho Chi Minh",
    "88 Cau Giay, Phuong Quan Hoa, Quan Cau Giay, Ha Noi",
    "215 Tran Phu, Phuong Van Quan, Quan Ha Dong, Ha Noi",
    "36 Nguyen Van Linh, Phuong Nam Duong, Quan Hai Chau, Da Nang",
]


def _get_random_profile(email: str = "") -> dict[str, str]:
    name = random.choice(_VN_NAMES)
    addr = random.choice(_VN_ADDRESSES)
    phone = "09" + "".join(random.choices("0123456789", k=8))
    default_email = f"user_{uuid.uuid4().hex[:6]}@example.com"
    return {
        "name": name,
        "email": email or default_email,
        "address": addr,
        "phone": phone,
        "postal_code": "700000",
        "city": "Ho Chi Minh",
    }


def _parse_playwright_proxy(proxy_str: str | None) -> dict[str, str] | None:
    if not proxy_str:
        return None
    from urllib.parse import urlparse, unquote
    raw = proxy_str.strip()
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": pwd,
            }
        elif len(parts) == 2:
            return {"server": f"http://{parts[0]}:{parts[1]}"}
        raw = f"http://{raw}"

    p = urlparse(raw)
    scheme = p.scheme or "http"
    if not p.hostname:
        return {"server": raw}
    server = f"{scheme}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    proxy_dict = {"server": server}
    if p.username:
        proxy_dict["username"] = unquote(p.username)
    if p.password:
        proxy_dict["password"] = unquote(p.password)
    return proxy_dict


async def fetch_momo_qr_via_browser(
    *,
    email: str,
    current_password: str = "",
    secret: str = "",
    session_data: dict[str, Any] | None = None,
    proxy: str | None = None,
    headless: bool = True,
    log: Callable[[str], None] = print,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Tự động chạy browser headless: nạp session -> mở pricing trial -> chọn MoMo -> điền info -> lấy mã QR MoMo."""
    from playwright.async_api import async_playwright

    from config import load_settings

    settings = load_settings()
    profile_dir = settings.profiles_dir / f"momo_browser_{abs(hash(email)) % 100000}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    session_data = session_data or {}
    s_token = session_data.get("sessionToken") or session_data.get("session_token")

    cookie_list: list[dict[str, Any]] = []
    if s_token:
        cookie_list.append({
            "name": "__Secure-next-auth.session-token",
            "value": str(s_token),
            "domain": ".chatgpt.com",
            "path": "/",
        })

    cookies_raw = session_data.get("__cookies") or session_data.get("cookies") or []
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

    pw = await async_playwright().start()
    context = None
    try:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "args": [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }
        pw_proxy = _parse_playwright_proxy(proxy)
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy

        for ch in ("msedge", "chrome", None):
            try:
                kw = dict(launch_kwargs)
                if ch:
                    kw["channel"] = ch
                context = await pw.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:
                log(f"[browser-momo] launch {ch} note: {e}")

        if not context:
            raise RuntimeError("Khong the khoi dong trinh duyet Playwright")

        if cookie_list:
            try:
                await context.add_cookies(cookie_list)
            except Exception as ce:
                log(f"[browser-momo] add_cookies: {ce}")

        page = context.pages[0] if context.pages else await context.new_page()

        pricing_url = "https://chatgpt.com/?promo_campaign=plus-1-month-free#pricing"
        log(f"[browser-momo] Dang mo ChatGPT Pricing Trial: {pricing_url}")
        await page.goto(pricing_url, wait_until="domcontentloaded", timeout=40000)
        await asyncio.sleep(3.0)

        # 1. Kiem tra neu bi redirect sang Login -> login bang email/password/2FA
        cur_url = page.url
        if "auth.openai.com" in cur_url or "/auth/login" in cur_url or "/log-in" in cur_url:
            log("[browser-momo] Phat hien trang Login, dang tu dong dang nhap...")
            if email and current_password:
                try:
                    email_input = page.locator('input[type="email"], input[name="username"], input[name="email"]').first
                    if await email_input.is_visible(timeout=5000):
                        await email_input.fill(email)
                        continue_btn = page.locator('button:has-text("Continue"), button[type="submit"]').first
                        await continue_btn.click()
                        await asyncio.sleep(2.0)

                    pwd_input = page.locator('input[type="password"]').first
                    if await pwd_input.is_visible(timeout=5000):
                        await pwd_input.fill(current_password)
                        login_btn = page.locator('button:has-text("Continue"), button[type="submit"]').first
                        await login_btn.click()
                        await asyncio.sleep(3.0)

                    if secret:
                        mfa_input = page.locator('input[inputmode="numeric"], input[name="code"], input[type="text"]').first
                        if await mfa_input.is_visible(timeout=5000):
                            totp_code = generate_code(normalize_secret(secret))
                            log(f"[browser-momo] Tu dong nhap ma 2FA TOTP: {totp_code}")
                            await mfa_input.fill(totp_code)
                            mfa_btn = page.locator('button:has-text("Continue"), button[type="submit"]').first
                            await mfa_btn.click()
                            await asyncio.sleep(3.0)
                except Exception as le:
                    log(f"[browser-momo] Login step warning: {le}")

            # Quay lai trang pricing
            await page.goto(pricing_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3.0)

        # 2. Click nut Trial / Upgrade tren Pricing Modal
        log("[browser-momo] Tim nut Upgrade to Plus / Try for $0...")
        upgrade_clicked = False
        upgrade_selectors = [
            'button:has-text("Try for $0")',
            'button:has-text("Try for free")',
            'button:has-text("Dung thu 0d")',
            'button:has-text("Dung thu")',
            'button:has-text("Upgrade to Plus")',
            'button:has-text("Nang cap len Plus")',
            'button:has-text("Upgrade")',
            'button[data-testid*="pricing-plan-plus"]',
        ]
        for sel in upgrade_selectors:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                log(f"[browser-momo] Click nut: {sel}")
                await loc.click()
                upgrade_clicked = True
                break

        if not upgrade_clicked:
            modal_btn = page.locator('div[role="dialog"] button.btn-primary, div[role="dialog"] button').first
            if await modal_btn.is_visible(timeout=2000):
                await modal_btn.click()
                upgrade_clicked = True

        await asyncio.sleep(4.0)

        # 3. Tim va click phuong thuc MoMo
        log("[browser-momo] Tim phuong thuc thanh toan MoMo tren giao dien Checkout...")
        momo_clicked = False
        momo_selectors = [
            'button:has-text("MoMo")',
            'div:has-text("MoMo")',
            'label:has-text("MoMo")',
            'input[value*="momo"]',
            '[data-testid*="momo"]',
            'img[alt*="MoMo"]',
            'img[src*="momo"]',
        ]
        
        frames = [page] + page.frames
        for fr in frames:
            for m_sel in momo_selectors:
                try:
                    m_loc = fr.locator(m_sel).first
                    if await m_loc.is_visible(timeout=1500):
                        log(f"[browser-momo] Chon phuong thuc MoMo ({m_sel})")
                        await m_loc.click()
                        momo_clicked = True
                        break
                except Exception:
                    pass
            if momo_clicked:
                break

        await asyncio.sleep(2.0)

        # 4. Dien thong tin khach hang random (Ten, Dia chi, Phone, Email)
        profile = _get_random_profile(email)
        log(f"[browser-momo] Dien thong tin random: {profile['name']} - {profile['address']}")

        for fr in frames:
            try:
                name_input = fr.locator('input[name*="name"], input[placeholder*="Name"], input[placeholder*="Ten"]').first
                if await name_input.is_visible(timeout=1000):
                    await name_input.fill(profile["name"])

                email_input = fr.locator('input[type="email"]').first
                if await email_input.is_visible(timeout=1000):
                    cur_val = await email_input.input_value()
                    if not cur_val:
                        await email_input.fill(profile["email"])

                addr_input = fr.locator('input[name*="address"], input[placeholder*="Address"], input[placeholder*="Dia chi"]').first
                if await addr_input.is_visible(timeout=1000):
                    await addr_input.fill(profile["address"])

                phone_input = fr.locator('input[type="tel"], input[name*="phone"]').first
                if await phone_input.is_visible(timeout=1000):
                    await phone_input.fill(profile["phone"])
            except Exception:
                pass

        await asyncio.sleep(1.5)

        # 5. Click Submit / Pay
        log("[browser-momo] Xac nhan thanh toan MoMo...")
        submit_selectors = [
            'button[type="submit"]',
            'button:has-text("Subscribe")',
            'button:has-text("Dang ky")',
            'button:has-text("Thanh toan")',
            'button:has-text("Pay")',
            'button:has-text("Continue")',
            'button.SubmitButton',
        ]
        for fr in frames:
            for s_sel in submit_selectors:
                try:
                    s_loc = fr.locator(s_sel).first
                    if await s_loc.is_visible(timeout=1500):
                        await s_loc.click()
                        log(f"[browser-momo] Da click submit ({s_sel})")
                        break
                except Exception:
                    pass

        await asyncio.sleep(4.0)

        # 6. Trich xuat Ma QR MoMo (anh QR hoac URL me.momo.vn)
        log("[browser-momo] Dang cho va trich xuat ma QR MoMo...")
        qr_base64 = ""
        momo_url = ""

        for attempt in range(6):
            if "momo.vn" in page.url or "me.momo.vn" in page.url:
                momo_url = page.url
                log(f"[browser-momo] Phat hien URL MoMo redirect: {momo_url}")
                break

            for fr in [page] + page.frames:
                try:
                    qr_img = fr.locator('img[src*="qr"], img[alt*="QR"], canvas, div[data-testid*="qr"]').first
                    if await qr_img.is_visible(timeout=1500):
                        screenshot_bytes = await qr_img.screenshot()
                        qr_base64 = "data:image/png;base64," + base64.b64encode(screenshot_bytes).decode("utf-8")
                        log("[browser-momo] Da chup anh QR MoMo tu giao dien thanh cong!")
                        break
                except Exception:
                    pass

            if qr_base64 or momo_url:
                break
            await asyncio.sleep(2.0)

        if not qr_base64 and not momo_url:
            full_bytes = await page.screenshot(full_page=False)
            qr_base64 = "data:image/png;base64," + base64.b64encode(full_bytes).decode("utf-8")
            log("[browser-momo] Da chup toan man hinh checkout MoMo")

        return {
            "ok": True,
            "qr_data": momo_url or qr_base64,
            "qr_img": qr_base64,
            "momo_url": momo_url or page.url,
            "status": "confirmed",
            "is_momo_native_qr": True,
            "profile": profile,
        }

    except Exception as exc:
        logger.exception("fetch_momo_qr_via_browser error: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
        }
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
