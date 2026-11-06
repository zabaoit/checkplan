"""Change ChatGPT email through Account UI (Camoufox) with direct API fallback and conservative recovery."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html as html_lib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urljoin

_CF_EPHEMERAL_COOKIES = frozenset({"__cf_bm", "_cfuvid", "cf_clearance"})
_DEFAULT_AUTH0_CLIENT_ID = "TdJIcbe16WoTHtN95nyywh5E4yOo6ItG"
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_CLIENT_BOOTSTRAP_RE = re.compile(
    r'<script[^>]+id=["\']client-bootstrap["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_AUTH0_CLIENT_ID_RE = re.compile(r'"auth0ClientId"\s*:\s*"([^"]+)"')
_CONFIRM_TEXT_RE = re.compile(
    r"email confirmed|email (?:address )?(?:has been |was )?(?:changed|updated)|successfully (?:changed|updated)",
    re.I,
)
_COOLDOWN_TEXT_RE = re.compile(r"you can[’']t change your email yet", re.I)
_ADD_PASSWORD_TEXT_RE = re.compile(r"add a password first", re.I)


class EmailChangeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        pending: bool = False,
        definitive: bool = False,
    ):
        super().__init__(message)
        self.error_kind = "email_verification_pending" if pending else "email_change_failed"
        self.definitive = definitive


def _redact_tokens(text: str) -> str:
    return _JWT_RE.sub("eyJ...[REDACTED]", str(text or ""))


def session_identity(session: dict[str, Any]) -> tuple[str, str]:
    user = session.get("user")
    if not isinstance(user, dict):
        raise EmailChangeError("Session thiếu user identity")
    email, user_id = user.get("email"), user.get("id")
    if not isinstance(email, str) or not email.strip() or not isinstance(user_id, str) or not user_id.strip():
        raise EmailChangeError("Session thiếu email hoặc user.id")
    return email.strip().casefold(), user_id


def session_cookies(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw = session.get("__cookies") or session.get("cookies") or []
    if isinstance(raw, dict):
        raw = [{"name": name, "value": value} for name, value in raw.items()]
    cookies = []
    if isinstance(raw, list):
        for cookie in raw:
            if not isinstance(cookie, dict) or not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
                raise EmailChangeError("Cookie session không hợp lệ")
            name = cookie["name"]
            item: dict[str, Any] = {"name": name, "value": cookie["value"]}
            for key in ("secure", "httpOnly"):
                if key in cookie:
                    item[key] = bool(cookie[key])
            same_site = str(cookie.get("sameSite") or "").strip().capitalize()
            if same_site in {"Strict", "Lax", "None"}:
                item["sameSite"] = same_site
            if name.startswith(("__Secure-", "__Host-")) or item.get("sameSite") == "None":
                item["secure"] = True
            item.update(domain=cookie.get("domain") or ".chatgpt.com", path=cookie.get("path") or "/")
            if isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0:
                item["expires"] = cookie["expires"]
            cookies.append(item)
    token = session.get("sessionToken") or session.get("session_token")
    if not cookies and isinstance(token, str) and token.strip():
        cookies.append({"name": "__Secure-next-auth.session-token", "value": token,
                        "domain": ".chatgpt.com", "path": "/", "secure": True, "httpOnly": True})
    if not cookies:
        raise EmailChangeError("Session cần cookies hoặc sessionToken; accessToken riêng không đủ")
    return cookies


def browser_cookies(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Prepare Playwright/Camoufox-safe cookies and strip curl_cffi ephemeral Cloudflare cookies."""
    cookies = session_cookies(session)
    filtered = [cookie for cookie in cookies if cookie.get("name") not in _CF_EPHEMERAL_COOKIES]
    selected = filtered or cookies
    out: list[dict[str, Any]] = []
    for cookie in selected:
        item = dict(cookie)
        name = str(item.get("name") or "")
        if name.startswith("__Host-"):
            host = str(item.pop("domain", "") or "chatgpt.com").lstrip(".") or "chatgpt.com"
            item.pop("path", None)
            item["url"] = f"https://{host}"
            item["secure"] = True
        elif name.startswith("__Secure-"):
            item["secure"] = True
        out.append(item)
    return out


def _extract_api_error_info(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    detail = data.get("detail")
    error = data.get("error")
    code = ""
    for candidate in (
        data.get("error_code"),
        data.get("code"),
        detail.get("error_code") if isinstance(detail, dict) else None,
        detail.get("code") if isinstance(detail, dict) else None,
        error.get("error_code") if isinstance(error, dict) else None,
        error.get("code") if isinstance(error, dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            code = candidate.strip()
            break
    message = ""
    for candidate in (
        detail if isinstance(detail, str) else None,
        detail.get("message") if isinstance(detail, dict) else None,
        detail.get("detail") if isinstance(detail, dict) else None,
        data.get("message"),
        error.get("message") if isinstance(error, dict) else None,
        error if isinstance(error, str) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            message = _redact_tokens(candidate.strip())[:180]
            break
    return code, message


def _is_reauth_required(status: int, data: Any, *, step: str) -> bool:
    code, _ = _extract_api_error_info(data)
    if code == "reauth_required":
        return True
    return step == "begin" and status == 401


def _explain_eligibility_denial(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    eligibility_type = str(data.get("eligibility_type") or "").strip().lower()
    if eligibility_type == "social":
        return (
            "Tài khoản đăng nhập qua Google/Microsoft/Apple chưa đặt mật khẩu "
            "(eligibility_type=social); cần thêm mật khẩu trước khi đổi email"
        )
    if data.get("eligible") is False:
        code, detail = _extract_api_error_info(data)
        reason = code or str(data.get("reason") or detail or "").strip()
        if reason == "email_change_account_too_new":
            return "Tài khoản mới tạo chưa đủ 24 giờ để đổi email (email_change_account_too_new)"
        if reason == "sensitive_action_cooldown":
            return "Tài khoản đang trong thời gian chờ bảo mật 24 giờ, chưa thể đổi email (sensitive_action_cooldown)"
        if reason == "email_change_rate_limited":
            return "Tài khoản đã đổi email tối đa 2 lần trong 24 giờ (email_change_rate_limited)"
        if reason == "email_change_limit_reached":
            return "Tài khoản đã chạm giới hạn 20 lần đổi email tối đa (email_change_limit_reached)"
        suffix = f" ({_redact_tokens(reason)[:120]})" if reason else ""
        return f"Tài khoản không đủ điều kiện đổi email trên ChatGPT (eligible=false){suffix}"
    return None


def _build_api_error(status: int, data: Any, *, step: str, challenge: bool = False) -> EmailChangeError:
    if challenge:
        label = "kiểm tra quyền đổi email" if step == "eligibility" else f"gọi {step} đổi email"
        return EmailChangeError(
            f"Cloudflare chặn yêu cầu khi {label} (HTTP {status}); chưa gửi yêu cầu đổi email",
            definitive=False,
        )
    code, detail = _extract_api_error_info(data)
    if code == "sensitive_action_cooldown":
        return EmailChangeError(
            "Tài khoản đang trong thời gian chờ bảo mật 24 giờ, chưa thể đổi email (sensitive_action_cooldown)",
            definitive=True,
        )
    if code == "email_change_account_too_new":
        return EmailChangeError(
            "Tài khoản mới tạo chưa đủ 24 giờ để đổi email (email_change_account_too_new)",
            definitive=True,
        )
    if code == "email_change_rate_limited":
        return EmailChangeError(
            "Tài khoản đã đổi email tối đa 2 lần trong 24 giờ (email_change_rate_limited)",
            definitive=True,
        )
    if code == "email_change_limit_reached":
        return EmailChangeError(
            "Tài khoản đã chạm giới hạn 20 lần đổi email tối đa (email_change_limit_reached)",
            definitive=True,
        )
    if status == 403 and ("already linked" in detail.lower() or step == "verify"):
        return EmailChangeError(
            "Email đích đã được liên kết với tài khoản ChatGPT khác",
            definitive=True,
        )
    if step == "verify" and status in (401, 422):
        return EmailChangeError(
            f"Mã OTP xác minh email không hợp lệ hoặc đã hết hạn (HTTP {status})",
            definitive=False,
        )
    if status == 429:
        return EmailChangeError(
            f"ChatGPT giới hạn tần suất yêu cầu đổi email (HTTP 429 tại {step})",
            definitive=False,
        )
    extra = f": {code or detail}" if (code or detail) else ""
    return EmailChangeError(
        f"ChatGPT từ chối {step} đổi email (HTTP {status}){extra}",
        definitive=False,
    )


def _extract_auth0_client_id(html_text: str) -> str:
    if not html_text:
        return _DEFAULT_AUTH0_CLIENT_ID
    match = _CLIENT_BOOTSTRAP_RE.search(html_text)
    if match:
        raw_json = html_lib.unescape(match.group(1).strip())
        try:
            bootstrap = json.loads(raw_json)
            auth = bootstrap.get("auth") if isinstance(bootstrap, dict) else None
            client_id = auth.get("auth0ClientId") if isinstance(auth, dict) else None
            if isinstance(client_id, str) and client_id.strip():
                return client_id.strip()
        except Exception:
            pass
    fallback = _AUTH0_CLIENT_ID_RE.search(html_text)
    if fallback and fallback.group(1).strip():
        return fallback.group(1).strip()
    return _DEFAULT_AUTH0_CLIENT_ID


async def _email_edit_control(page, email):
    from playwright.async_api import TimeoutError as BrowserTimeout

    labeled = page.get_by_role("button", name=re.compile(
        rf"^(?:edit|change|update)\s+e-?mail:\s*{re.escape(email)}$", re.I)).locator("visible=true")
    source = page.get_by_text(email, exact=True).locator("visible=true")
    try:
        await source.first.wait_for(state="visible", timeout=25000)
    except BrowserTimeout:
        if await labeled.count() == 1:
            return labeled
        raise EmailChangeError("Trang Account chưa hiển thị email tài khoản (timeout tải dữ liệu Account)") from None
    if await source.count() != 1:
        try:
            await labeled.first.wait_for(state="visible", timeout=1500)
        except BrowserTimeout:
            pass
        labeled_count = await labeled.count()
        if labeled_count == 1:
            return labeled
        if labeled_count > 1:
            raise EmailChangeError("Không xác định duy nhất nút Change email của tài khoản nguồn")
        raise EmailChangeError("Không xác định duy nhất dòng email nguồn")
    # Email may itself sit inside the clickable row rather than beside a button.
    control = source.locator("xpath=ancestor-or-self::*[self::button or @role='button'][1]").locator("visible=true")
    if await control.count() == 1:
        return control
    row = source.locator("xpath=ancestor::*[.//*[normalize-space(.)='Email' or normalize-space(.)='Email address' or normalize-space(.)='Địa chỉ email']][1]")
    if await row.count() != 1:
        row = source.locator("xpath=ancestor::*[.//button or .//*[@role='button']][1]")
    try:
        await labeled.or_(row.locator('button:visible, [role="button"]:visible, svg:visible')).first.wait_for(state="visible", timeout=15000)
    except BrowserTimeout:
        raise EmailChangeError("Trang Account không hiển thị nút sửa trong dòng email") from None
    labeled_count = await labeled.count()
    if labeled_count > 1:
        raise EmailChangeError("Không xác định duy nhất nút Change email của tài khoản nguồn")
    if labeled_count == 1:
        return labeled
    controls = row.locator('button:visible, [role="button"]:visible')
    named = row.get_by_role("button", name=re.compile(r"(?:edit|change|update)\s+(?:email|e-mail)|(?:sửa|đổi|chỉnh sửa)\s+(?:email|địa chỉ email)", re.I)).locator("visible=true")
    if await named.count() == 1:
        return named
    count = await controls.count()
    if count == 1:
        return controls
    # ponytail: one icon in the labeled email row supports non-semantic pencil controls;
    # opening the email form must succeed before any mailbox request or remote change.
    icons = row.locator("svg:visible")
    if count == 0 and await icons.count() == 1:
        return icons
    raise EmailChangeError(f"Không xác định duy nhất nút sửa email (controls={count}, icons={await icons.count()})")


async def _open_account_page(page, log):
    response = await page.goto("https://chatgpt.com/settings/account", wait_until="domcontentloaded", timeout=45000)
    if response is None:
        raise EmailChangeError("Trang Account không có phản hồi")
    if response.status == 403 and response.headers.get("cf-mitigated") == "challenge":
        log("[email] Cloudflare chặn browser tự động (HTTP 403); dừng trước đổi email.")
        raise EmailChangeError("Cloudflare chặn browser tự động (HTTP 403); chưa gửi yêu cầu đổi email")
    if response.status != 200:
        raise EmailChangeError(f"Không tải được trang Account (HTTP {response.status})")


@asynccontextmanager
async def _launch_email_browser_context(settings, profile_dir, parsed_proxy, log):
    from playwright.async_api import async_playwright

    playwright_patched = getattr(async_playwright, "__module__", "") != "playwright.async_api"
    engine = str(getattr(settings, "browser_engine", "camoufox") or "camoufox").strip().lower()

    if not playwright_patched and engine == "camoufox":
        cf = None
        ctx = None
        try:
            from camoufox.async_api import AsyncCamoufox
            from user_agent_profile import CAMOUFOX_OS

            if parsed_proxy and getattr(settings, "runtime_dir", None):
                try:
                    from browser_phase import _ensure_geoip_cache

                    _ensure_geoip_cache(settings.runtime_dir, log=log)
                except Exception:
                    pass

            cf_kwargs: dict[str, Any] = {
                "headless": bool(settings.browser_headless),
                "persistent_context": True,
                "user_data_dir": str(profile_dir),
                "os": list(CAMOUFOX_OS),
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
                "geoip": bool(parsed_proxy),
            }
            if parsed_proxy:
                cf_kwargs["proxy"] = parsed_proxy
            cf = AsyncCamoufox(**cf_kwargs)
            ctx = await cf.__aenter__()
            log("[email] Đã mở Camoufox browser cho trang Account")
        except Exception as exc:
            if cf is not None:
                try:
                    await cf.__aexit__(None, None, None)
                except Exception:
                    pass
            log(f"[email] Không mở được Camoufox ({type(exc).__name__}); chuyển sang Chromium")
            cf = None
            ctx = None

        if ctx is not None and cf is not None:
            try:
                yield ctx
            finally:
                try:
                    await cf.__aexit__(None, None, None)
                except Exception:
                    pass
            return

    async with async_playwright() as pw:
        options: dict[str, Any] = dict(
            user_data_dir=str(profile_dir),
            headless=settings.browser_headless,
            locale="en-US",
        )
        if parsed_proxy:
            options["proxy"] = parsed_proxy
        if not playwright_patched:
            options["args"] = ["--disable-blink-features=AutomationControlled"]
        context = None
        for channel in ("msedge", "chrome", None):
            try:
                context = await pw.chromium.launch_persistent_context(
                    **options,
                    **({"channel": channel} if channel else {}),
                )
                break
            except Exception:
                continue
        if context is None:
            raise EmailChangeError("Không mở được browser đổi email")
        try:
            yield context
        finally:
            await context.close()


async def _complete_browser_reauth(
    page,
    *,
    password: str,
    secret: str,
    log: Callable[[str], None],
) -> None:
    if not password:
        raise EmailChangeError("ChatGPT yêu cầu nhập lại mật khẩu nhưng thiếu password")
    from totp_helper import generate_code, time_remaining

    log("[email] ChatGPT chuyển sang trang xác thực lại (reauth); đang nhập password/2FA...")
    deadline = asyncio.get_running_loop().time() + 45.0
    password_submitted = False
    mfa_submitted = False

    while asyncio.get_running_loop().time() < deadline:
        current_url = page.url or ""
        if "chatgpt.com/settings/account" in current_url and (password_submitted or mfa_submitted):
            log("[email] Đã hoàn tất xác thực lại, quay về trang Account")
            return

        if not password_submitted:
            pw_input = page.locator('input[type="password"]:visible, input[name="password"]:visible')
            if await pw_input.count() == 1:
                await pw_input.fill(password)
                submit = page.locator('button[type="submit"]:visible')
                if await submit.count() >= 1:
                    await submit.first.click(timeout=8000)
                else:
                    await pw_input.press("Enter")
                password_submitted = True
                await asyncio.sleep(1.0)
                continue

        if secret and not mfa_submitted and "auth.openai.com" in current_url:
            mfa_input = page.locator(
                'input[autocomplete="one-time-code"]:visible, '
                'input[inputmode="numeric"]:visible, '
                'input[name="code"]:visible'
            )
            if await mfa_input.count() == 1:
                rem = time_remaining()
                if rem <= 4:
                    await asyncio.sleep(rem + 0.5)
                code = generate_code(secret)
                await mfa_input.fill(code)
                submit = page.locator('button[type="submit"]:visible')
                if await submit.count() >= 1:
                    await submit.first.click(timeout=8000)
                else:
                    await mfa_input.press("Enter")
                mfa_submitted = True
                await asyncio.sleep(1.0)
                continue

        await asyncio.sleep(0.4)

    raise EmailChangeError("Quá thời gian xác thực lại (reauth) trên trình duyệt")


async def _wait_for_otp_prompt_after_send(
    page,
    *,
    api_state: dict[str, dict[str, Any]],
    password: str,
    secret: str,
    log: Callable[[str], None],
):
    otp_selector = (
        'input[autocomplete="one-time-code"]:visible, '
        'input[inputmode="numeric"]:visible, '
        'input[name="verification_code"]:visible'
    )
    reauth_handled = False
    deadline = asyncio.get_running_loop().time() + 25.0

    while asyncio.get_running_loop().time() < deadline:
        current_url = page.url or ""
        if "auth.openai.com" in current_url and not reauth_handled:
            reauth_handled = True
            await _complete_browser_reauth(page, password=password, secret=secret, log=log)
            deadline = asyncio.get_running_loop().time() + 25.0
            continue

        otp_field = page.locator(otp_selector)
        if await otp_field.count() > 0:
            return otp_field

        if await page.get_by_text(_COOLDOWN_TEXT_RE).locator("visible=true").count() > 0:
            raise EmailChangeError(
                "Tài khoản đang trong thời gian chờ bảo mật 24 giờ, chưa thể đổi email (sensitive_action_cooldown)",
                definitive=True,
            )

        begin = api_state.get("begin")
        if begin and begin.get("ready"):
            status = int(begin.get("status") or 0)
            body = begin.get("body")
            if status >= 400:
                if _is_reauth_required(status, body, step="begin") and not reauth_handled:
                    await asyncio.sleep(0.5)
                    continue
                raise _build_api_error(
                    status,
                    body,
                    step="begin",
                    challenge=bool(begin.get("challenge")),
                )

        alert = page.locator('[role="alert"]:visible')
        if await alert.count() > 0:
            alert_text = _redact_tokens((await alert.first.inner_text()).strip())
            if alert_text:
                lower = alert_text.lower()
                definitive = any(
                    marker in lower
                    for marker in (
                        "24 hours",
                        "twice within 24 hours",
                        "20-email-change",
                        "already linked",
                        "not formatted correctly",
                    )
                )
                raise EmailChangeError(
                    f"ChatGPT từ chối gửi mã đổi email: {alert_text[:180]}",
                    definitive=definitive,
                )

        await asyncio.sleep(0.25)

    raise EmailChangeError("Không thấy form nhập OTP sau khi bấm gửi email xác minh")


async def _change_in_browser(
    *,
    email,
    target_email,
    session,
    proxy,
    mailbox,
    save,
    log=None,
    password: str = "",
    secret: str = "",
):
    log = log or (lambda _: None)
    from browser_momo import _parse_playwright_proxy
    from config import load_settings

    cookies = browser_cookies(session)
    settings = load_settings()
    log("[email] Mở browser ẩn" if settings.browser_headless else "[email] Mở browser hiển thị")
    profile_dir = settings.profiles_dir / f"email_change_{uuid.uuid4().hex}"
    profile_dir.mkdir(parents=True, exist_ok=False)
    pending_tasks: set[asyncio.Task[Any]] = set()
    try:
        parsed_proxy = _parse_playwright_proxy(proxy)
        async with _launch_email_browser_context(settings, profile_dir, parsed_proxy, log) as context:
            await context.add_cookies(cookies)
            page = context.pages[0] if context.pages else await context.new_page()
            api_state: dict[str, dict[str, Any]] = {}

            async def _capture_response(key: str, response) -> None:
                entry = api_state[key]
                try:
                    entry["body"] = await response.json()
                except Exception:
                    entry["body"] = None
                finally:
                    entry["ready"] = True

            def _observe_response(response) -> None:
                base_url = response.url.split("?")[0]
                for key, suffix in (
                    ("eligibility", "/backend-api/accounts/change_email/eligibility"),
                    ("begin", "/backend-api/accounts/change_email/begin"),
                    ("verify", "/backend-api/accounts/change_email/verify"),
                ):
                    if base_url == f"https://chatgpt.com{suffix}":
                        api_state[key] = {
                            "status": response.status,
                            "challenge": response.headers.get("cf-mitigated") == "challenge",
                            "body": None,
                            "ready": False,
                        }
                        task = asyncio.create_task(_capture_response(key, response))
                        pending_tasks.add(task)
                        task.add_done_callback(pending_tasks.discard)

            page.on("response", _observe_response)
            await _open_account_page(page, log)
            live = await page.evaluate("""async () => {
                const r = await fetch('/api/auth/session', {credentials:'include'});
                return r.ok ? await r.json() : null;
            }""")
            source_email, user_id = session_identity(live or {})
            if source_email != email.casefold():
                raise EmailChangeError("Identity session không khớp email nguồn", definitive=True)
            log("[email] Session hợp lệ, đã đăng nhập đúng tài khoản")
            await save("prepared", user_id)

            try:
                edit = await _email_edit_control(page, email)
            except EmailChangeError:
                if pending_tasks:
                    await asyncio.gather(*list(pending_tasks), return_exceptions=True)
                elig = api_state.get("eligibility") or {}
                denial = _explain_eligibility_denial(elig.get("body"))
                if denial:
                    raise EmailChangeError(denial, definitive=True) from None
                status = int(elig.get("status") or 0)
                if status < 400:
                    raise
                reason = "Cloudflare chặn browser tự động khi" if elig.get("challenge") else "ChatGPT từ chối"
                raise EmailChangeError(
                    f"{reason} kiểm tra quyền đổi email (HTTP {status}); chưa gửi yêu cầu đổi email"
                ) from None

            if not await edit.is_enabled():
                raise EmailChangeError("Nút sửa email đang bị khóa")
            await edit.click(timeout=8000)

            field = page.locator('input[type="email"]:visible')
            pw_required = page.get_by_text(_ADD_PASSWORD_TEXT_RE).locator("visible=true")
            from playwright.async_api import TimeoutError as BrowserTimeout

            try:
                await field.or_(pw_required).first.wait_for(state="visible", timeout=15000)
            except BrowserTimeout:
                raise EmailChangeError("Không mở được hộp thoại nhập email mới") from None
            if await pw_required.count() > 0:
                raise EmailChangeError(
                    "Tài khoản đăng nhập qua Google/Microsoft/Apple chưa đặt mật khẩu; cần thêm mật khẩu trước khi đổi email",
                    definitive=True,
                )
            if await field.count() != 1:
                raise EmailChangeError("Không xác định duy nhất ô email mới")
            log("[email] Đã mở form đổi email")
            await field.fill(target_email)

            send = page.get_by_role(
                "button",
                name=re.compile(r"^(?:continue|send code|send verification (?:code|email)|save|change email|update email)$", re.I),
            ).locator("visible=true")
            if await send.count() != 1:
                raise EmailChangeError("Không xác định duy nhất nút gửi email mới")

            await mailbox.prepare()
            api_state.pop("begin", None)
            started_at = datetime.now(timezone.utc)
            await send.click(timeout=8000)

            otp_field = await _wait_for_otp_prompt_after_send(
                page,
                api_state=api_state,
                password=password,
                secret=secret,
                log=log,
            )
            await save("submitted", user_id)
            log("[email] Đã gửi yêu cầu, đang chờ OTP Congas")
            try:
                otp = str(
                    await mailbox.poll_otp(
                        recipient=target_email,
                        started_at=started_at,
                        timeout_seconds=180,
                        poll_interval_seconds=4,
                        log=log,
                        source_email=email,
                    )
                ).strip()
            except TimeoutError:
                raise EmailChangeError(
                    f"Quá 180s không nhận được mã OTP gửi tới {target_email} trên Congas"
                ) from None

            count = await otp_field.count()
            if count == 1:
                await otp_field.fill(otp)
            elif count == 6 and len(otp) == 6:
                for index, digit in enumerate(otp):
                    await otp_field.nth(index).fill(digit)
            else:
                raise EmailChangeError("Không xác định ô OTP email")

            confirm = page.get_by_role(
                "button",
                name=re.compile(r"^(?:verify|verify email|confirm|confirm email|continue|save)$", re.I),
            ).locator("visible=true")
            if await confirm.count() > 1:
                raise EmailChangeError("Không xác định duy nhất nút xác minh email")
            api_state.pop("verify", None)
            if await confirm.count() == 1:
                for _ in range(20):
                    if await confirm.is_enabled():
                        break
                    await asyncio.sleep(0.1)
                await confirm.click(timeout=8000)

            # ponytail: auto-submit accepted only by fresh identity login below, never UI text alone.
            confirmed = False
            verify_deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < verify_deadline:
                verify_obs = api_state.get("verify")
                if verify_obs:
                    v_status = int(verify_obs.get("status") or 0)
                    if 200 <= v_status < 300:
                        confirmed = True
                        break
                    if verify_obs.get("ready") and v_status >= 400:
                        raise _build_api_error(
                            v_status,
                            verify_obs.get("body"),
                            step="verify",
                            challenge=bool(verify_obs.get("challenge")),
                        )
                try:
                    if await page.get_by_text(_CONFIRM_TEXT_RE).count() > 0:
                        confirmed = True
                        break
                except Exception:
                    current_url = page.url or ""
                    if "/auth/logout" in current_url or "auth.openai.com" in current_url:
                        confirmed = True
                        break
                current_url = page.url or ""
                if "/auth/logout" in current_url:
                    confirmed = True
                    break
                await asyncio.sleep(0.2)

            if confirmed:
                await save("changed_pending_verify", user_id)
    finally:
        if pending_tasks:
            await asyncio.gather(*list(pending_tasks), return_exceptions=True)
        shutil.rmtree(profile_dir, ignore_errors=True)


def _backend_api_headers(
    *,
    access_token: str,
    device_id: str,
    target_path: str,
    account_id: str = "",
) -> dict[str, str]:
    from user_agent_profile import (
        SEC_CH_UA,
        SEC_CH_UA_MOBILE,
        SEC_CH_UA_PLATFORM,
        WINDOWS_USER_AGENT,
    )

    headers = {
        "User-Agent": WINDOWS_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {access_token}",
        "OAI-Device-Id": device_id,
        "OAI-Language": "en-US",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/settings/account",
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _is_cf_mitigated(resp: Any) -> bool:
    headers = getattr(resp, "headers", None) or {}
    try:
        if str(headers.get("cf-mitigated") or "").lower() == "challenge":
            return True
    except Exception:
        pass
    if int(getattr(resp, "status_code", 0) or 0) in (403, 503):
        body = str(getattr(resp, "text", "") or "").lower()
        if "challenge-platform" in body or "just a moment" in body:
            return True
    return False


async def _follow_to_nextauth_callback(
    sess: Any,
    continue_url: str,
    log: Callable[[str], None],
) -> None:
    from change_password import _chatgpt_headers, nextauth_callback

    current = continue_url
    for _ in range(8):
        if not current:
            break
        if current.startswith("/"):
            current = urljoin("https://auth.openai.com", current)
        if "chatgpt.com/api/auth/callback/openai" in current:
            await nextauth_callback(sess, current, log)
            return
        resp = await sess.get(
            current,
            headers=_chatgpt_headers(referer="https://auth.openai.com/"),
            allow_redirects=False,
            timeout=30.0,
        )
        if resp.status_code not in (301, 302, 303, 307, 308):
            break
        location = str(resp.headers.get("Location") or resp.headers.get("location") or "")
        current = urljoin(current, location) if location else ""
    raise EmailChangeError("Reauth không trả về callback phiên ChatGPT")


async def _reauth_email_change_api(
    sess: Any,
    *,
    email: str,
    password: str,
    secret: str,
    device_id: str,
    log: Callable[[str], None],
) -> None:
    from change_password import (
        ChangePasswordError,
        _chatgpt_headers,
        _get_with_warm_retry,
        _post_with_warm_retry,
        complete_mfa,
        factor_id,
        get_cookie,
        open_password_page,
        password_verify,
        response_json,
        store_client_auth_session,
    )

    if not password:
        raise EmailChangeError("ChatGPT yêu cầu reauth mật khẩu nhưng thiếu password")

    try:
        csrf_resp = await _get_with_warm_retry(
            sess,
            "https://chatgpt.com/api/auth/csrf",
            headers={**_chatgpt_headers(referer="https://chatgpt.com/settings/account"), "Accept": "application/json"},
            log=log,
            label="email-reauth csrf",
            timeout=30.0,
        )
        if csrf_resp.status_code != 200:
            raise EmailChangeError(f"Không lấy được CSRF để reauth đổi email (HTTP {csrf_resp.status_code})")
        csrf_token = str(response_json(csrf_resp).get("csrfToken") or "")
        if not csrf_token:
            raise EmailChangeError("Thiếu csrfToken khi reauth đổi email")

        oai_did = get_cookie(sess, "oai-did") or device_id
        query = urlencode({
            "reauth": "password",
            "max_age": "0",
            "login_hint": email,
            "ext-oai-did": oai_did,
        })
        signin_resp = await _post_with_warm_retry(
            sess,
            f"https://chatgpt.com/api/auth/signin/openai?{query}",
            headers={
                **_chatgpt_headers(referer="https://chatgpt.com/settings/account"),
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://chatgpt.com",
            },
            log=log,
            label="email-reauth signin",
            data={
                "callbackUrl": "https://chatgpt.com/settings/account",
                "csrfToken": csrf_token,
                "json": "true",
            },
            timeout=30.0,
        )
        if signin_resp.status_code != 200:
            raise EmailChangeError(f"Khởi tạo reauth đổi email thất bại (HTTP {signin_resp.status_code})")
        authorize_url = str(response_json(signin_resp).get("url") or "")
        if "auth.openai.com" not in authorize_url:
            raise EmailChangeError("NextAuth reauth không trả về URL auth.openai.com")

        pw_page_url = await open_password_page(sess, authorize_url, log)
        verify_resp = await password_verify(sess, password, referer=pw_page_url)
        verify_data = response_json(verify_resp)
        store_client_auth_session(sess, verify_data)
        if verify_resp.status_code != 200:
            raise EmailChangeError(f"Xác thực lại mật khẩu thất bại (HTTP {verify_resp.status_code})")

        continue_url = str(verify_data.get("continue_url") or "")
        page_info = verify_data.get("page") if isinstance(verify_data.get("page"), dict) else {}
        page_type = str(page_info.get("type") or "")
        needs_mfa = "mfa" in page_type.lower() or "/mfa-challenge/" in continue_url.lower()
        if needs_mfa:
            fid = factor_id(verify_data)
            if not fid:
                raise EmailChangeError("Reauth yêu cầu 2FA nhưng thiếu factor_id")
            mfa_data = await complete_mfa(
                sess,
                factor_id=fid,
                secret=secret,
                step="email-reauth",
                log=log,
                consume_callback=False,
            )
            mfa_continue = str(mfa_data.get("continue_url") or "")
            await _follow_to_nextauth_callback(sess, mfa_continue, log)
        else:
            await _follow_to_nextauth_callback(sess, continue_url, log)
    except ChangePasswordError as exc:
        raise EmailChangeError(f"Reauth đổi email thất bại: {_redact_tokens(str(exc))}") from None


async def _change_via_api(
    *,
    email: str,
    password: str,
    secret: str,
    target_email: str,
    session: dict[str, Any],
    proxy: str | None,
    mailbox: Any,
    save: Callable[[str, str], Awaitable[None]],
    log: Callable[[str], None] | None = None,
) -> None:
    log = log or (lambda _: None)
    from curl_cffi.requests import AsyncSession
    from change_password import (
        _chatgpt_headers,
        _get_with_warm_retry,
        get_cookie,
        normalize_session_cookies,
        response_json,
        set_cookies,
    )
    from user_agent_profile import CURL_IMPERSONATE_PRIMARY

    raw_cookies = session_cookies(session)
    normalized = normalize_session_cookies(raw_cookies) or raw_cookies
    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with AsyncSession(impersonate=CURL_IMPERSONATE_PRIMARY, proxies=proxies) as sess:
        set_cookies(sess, normalized)
        device_id = get_cookie(sess, "oai-did") or str(uuid.uuid4())

        account_page_resp = await _get_with_warm_retry(
            sess,
            "https://chatgpt.com/settings/account",
            headers=_chatgpt_headers(),
            log=log,
            label="account settings",
            timeout=30.0,
        )
        if account_page_resp.status_code >= 400:
            raise _build_api_error(
                account_page_resp.status_code,
                response_json(account_page_resp),
                step="account_page",
                challenge=_is_cf_mitigated(account_page_resp),
            )
        auth0_client_id = _extract_auth0_client_id(str(getattr(account_page_resp, "text", "") or ""))
        device_id = get_cookie(sess, "oai-did") or device_id

        async def _load_live_auth() -> tuple[str, str, str]:
            sess_resp = await _get_with_warm_retry(
                sess,
                "https://chatgpt.com/api/auth/session",
                headers={**_chatgpt_headers(referer="https://chatgpt.com/settings/account"), "Accept": "application/json"},
                log=log,
                label="auth session",
                timeout=30.0,
            )
            if sess_resp.status_code != 200:
                raise EmailChangeError(f"Không đọc được session trên trang Account (HTTP {sess_resp.status_code})")
            live_data = response_json(sess_resp)
            live_email, live_uid = session_identity(live_data)
            if live_email != email.casefold():
                raise EmailChangeError("Identity session không khớp email nguồn", definitive=True)
            token = str(live_data.get("accessToken") or session.get("accessToken") or "").strip()
            if not token:
                raise EmailChangeError("Session thiếu accessToken để gọi API đổi email")
            acc = live_data.get("account")
            acc_id = str(acc.get("id") or "").strip() if isinstance(acc, dict) else ""
            return live_uid, token, acc_id

        user_id, access_token, account_id = await _load_live_auth()
        await save("prepared", user_id)

        elig_path = "/backend-api/accounts/change_email/eligibility"
        elig_resp = await _get_with_warm_retry(
            sess,
            f"https://chatgpt.com{elig_path}",
            headers=_backend_api_headers(
                access_token=access_token,
                device_id=device_id,
                target_path=elig_path,
                account_id=account_id,
            ),
            log=log,
            label="change_email eligibility",
            timeout=30.0,
        )
        elig_data = response_json(elig_resp)
        if elig_resp.status_code != 200:
            raise _build_api_error(
                elig_resp.status_code,
                elig_data,
                step="eligibility",
                challenge=_is_cf_mitigated(elig_resp),
            )
        denial = _explain_eligibility_denial(elig_data)
        if denial:
            raise EmailChangeError(denial, definitive=True)
        if elig_data.get("eligible") is not True:
            raise EmailChangeError("Tài khoản không đủ điều kiện đổi email (eligible != true)", definitive=True)

        social_user = str(elig_data.get("eligibility_type") or "").strip().lower() == "social_password"
        await mailbox.prepare()
        started_at = datetime.now(timezone.utc)

        begin_path = "/backend-api/accounts/change_email/begin"
        begin_body: dict[str, Any] = {
            "email": target_email,
            "auth0_client_id": auth0_client_id,
        }
        if social_user:
            begin_body["remove_social_subs"] = True

        async def _post_begin(token: str) -> Any:
            return await sess.post(
                f"https://chatgpt.com{begin_path}",
                headers={
                    **_backend_api_headers(
                        access_token=token,
                        device_id=device_id,
                        target_path=begin_path,
                        account_id=account_id,
                    ),
                    "Content-Type": "application/json",
                },
                json=begin_body,
                timeout=30.0,
            )

        begin_resp = await _post_begin(access_token)
        begin_data = response_json(begin_resp)
        if _is_reauth_required(begin_resp.status_code, begin_data, step="begin"):
            log("[email] ChatGPT yêu cầu xác thực lại mật khẩu (reauth_required); đang reauth qua API...")
            await _reauth_email_change_api(
                sess,
                email=email,
                password=password,
                secret=secret,
                device_id=device_id,
                log=log,
            )
            reauth_uid, access_token, account_id = await _load_live_auth()
            if reauth_uid != user_id:
                raise EmailChangeError("user.id sau reauth không khớp tài khoản nguồn", definitive=True)
            started_at = datetime.now(timezone.utc)
            begin_resp = await _post_begin(access_token)
            begin_data = response_json(begin_resp)

        if not (200 <= begin_resp.status_code < 300):
            raise _build_api_error(
                begin_resp.status_code,
                begin_data,
                step="begin",
                challenge=_is_cf_mitigated(begin_resp),
            )

        log("[email] Đã gửi yêu cầu đổi email qua API, đang chờ OTP Congas")
        try:
            otp = str(
                await mailbox.poll_otp(
                    recipient=target_email,
                    started_at=started_at,
                    timeout_seconds=180,
                    poll_interval_seconds=4,
                    log=log,
                    source_email=email,
                )
            ).strip()
        except TimeoutError:
            raise EmailChangeError(
                f"Quá 180s không nhận được mã OTP gửi tới {target_email} trên Congas",
                definitive=True,
            ) from None

        await save("submitted", user_id)
        verify_path = "/backend-api/accounts/change_email/verify"
        verify_body: dict[str, Any] = {
            "email": target_email,
            "code": otp,
        }
        if social_user:
            verify_body["remove_social_subs"] = True

        verify_resp = await sess.post(
            f"https://chatgpt.com{verify_path}",
            headers={
                **_backend_api_headers(
                    access_token=access_token,
                    device_id=device_id,
                    target_path=verify_path,
                    account_id=account_id,
                ),
                "Content-Type": "application/json",
            },
            json=verify_body,
            timeout=30.0,
        )
        verify_data = response_json(verify_resp)
        if not (200 <= verify_resp.status_code < 300):
            raise _build_api_error(
                verify_resp.status_code,
                verify_data,
                step="verify",
                challenge=_is_cf_mitigated(verify_resp),
            )

        await save("changed_pending_verify", user_id)
        log("[email] Đã xác nhận OTP đổi email qua API thành công")


def _sanitize_exception_reason(
    exc: BaseException,
    *,
    password: str = "",
    secret: str = "",
    mailbox_key: str = "",
) -> str:
    if isinstance(exc, TimeoutError):
        raw = str(exc).strip() or "Quá thời gian chờ phản hồi (timeout)"
    else:
        raw = str(exc).strip() or type(exc).__name__
    text = _redact_tokens(raw)
    for sensitive in (
        password,
        password.strip(),
        secret,
        secret.replace(" ", ""),
        mailbox_key,
        mailbox_key.strip(),
    ):
        if sensitive and len(sensitive) >= 4:
            text = text.replace(sensitive, "[REDACTED]")
            text = text.replace(sensitive.upper(), "[REDACTED]")
            text = text.replace(sensitive.lower(), "[REDACTED]")
    return text[:220]


_DEFAULT_CHANGE_IN_BROWSER = _change_in_browser


async def change_email(
    *, email: str, password: str, secret: str, target_email: str,
    mailbox_email: str, mailbox_key: str,
    proxy: str | None = None, log: Callable[[str], None] | None = None,
    stage: str = "prepared", expected_user_id: str = "",
    checkpoint: Callable[[str, str], Awaitable[None]],
) -> dict[str, Any]:
    from congas_mail import CongasMailbox
    from session_phase import get_session_pure_request

    log = log or (lambda _: None)
    if stage not in {"prepared", "submitted", "changed_pending_verify", "verified"}:
        raise EmailChangeError("Trạng thái đổi email không hợp lệ")
    if email.casefold() == target_email.casefold() or "@" not in target_email:
        raise EmailChangeError("Email đích không hợp lệ")
    user_id = expected_user_id
    last_checkpoint: tuple[str, str] | None = None

    async def save(next_stage: str, identity: str):
        nonlocal stage, user_id, last_checkpoint
        if user_id and user_id != identity:
            raise EmailChangeError("user.id gốc không khớp session đang đăng nhập", definitive=True)
        stage, user_id = next_stage, identity
        if last_checkpoint == (next_stage, identity):
            return
        await checkpoint(next_stage, identity)
        last_checkpoint = (next_stage, identity)

    try:
        if stage == "prepared":
            log("[email] Đăng nhập qua Get Session trước khi đổi email")
            session = await get_session_pure_request(
                email=email, password=password, secret=secret, proxy=proxy, log=log)
            source_email, source_id = session_identity(session)
            if source_email != email.casefold():
                raise EmailChangeError("Get Session trả về tài khoản nguồn không khớp", definitive=True)
            if session.get("__mfa_verified") is not True:
                raise EmailChangeError("Get Session chưa xác minh TOTP cũ; dừng trước khi đổi email", definitive=True)
            session_cookies(session)
            await save("prepared", source_id)
            log("[email] Get Session hoàn tất; mở Account để tìm nút đổi email")
            async with CongasMailbox(mailbox_email, mailbox_key, log=log) as mailbox:
                browser_patched = _change_in_browser is not _DEFAULT_CHANGE_IN_BROWSER
                if browser_patched:
                    try:
                        await _change_in_browser(
                            email=email,
                            target_email=target_email,
                            session=session,
                            proxy=proxy,
                            mailbox=mailbox,
                            save=save,
                            log=log,
                            password=password,
                            secret=secret,
                        )
                    except EmailChangeError as browser_exc:
                        if stage != "prepared" or browser_exc.definitive:
                            raise
                        log(f"[email] Browser Account UI gặp trở ngại ({browser_exc}); chuyển sang API đổi email trực tiếp")
                        await _change_via_api(
                            email=email,
                            password=password,
                            secret=secret,
                            target_email=target_email,
                            session=session,
                            proxy=proxy,
                            mailbox=mailbox,
                            save=save,
                            log=log,
                        )
                else:
                    try:
                        await _change_via_api(
                            email=email,
                            password=password,
                            secret=secret,
                            target_email=target_email,
                            session=session,
                            proxy=proxy,
                            mailbox=mailbox,
                            save=save,
                            log=log,
                        )
                    except Exception as api_exc:
                        definitive = bool(getattr(api_exc, "definitive", False))
                        from congas_mail import CongasMailError

                        if stage != "prepared" or definitive or isinstance(api_exc, CongasMailError):
                            raise
                        reason = _sanitize_exception_reason(
                            api_exc,
                            password=password,
                            secret=secret,
                            mailbox_key=mailbox_key,
                        )
                        log(f"[email] API đổi email gặp trở ngại ({reason}); chuyển sang Camoufox Account UI")
                        await _change_in_browser(
                            email=email,
                            target_email=target_email,
                            session=session,
                            proxy=proxy,
                            mailbox=mailbox,
                            save=save,
                            log=log,
                            password=password,
                            secret=secret,
                        )
        if not user_id:
            raise EmailChangeError("Thiếu user.id gốc để xác minh")
        log("[email] Đang xác minh email mới và 2FA cũ")
        fresh = await get_session_pure_request(email=target_email, password=password, secret=secret,
                                               proxy=proxy, log=log)
        fresh_email, fresh_id = session_identity(fresh)
        if fresh_email != target_email.casefold() or fresh_id != user_id:
            raise EmailChangeError("Identity sau đổi email không khớp")
        if fresh.get("__mfa_verified") is not True:
            raise EmailChangeError("Đăng nhập mới chưa xác minh challenge TOTP cũ")
        await save("verified", user_id)
        return {"email": target_email, "secret": secret, "login_verified": True, "user_id": user_id}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        pending = stage in {"submitted", "changed_pending_verify", "verified"}
        message = "Email có thể đã đổi; Retry chỉ xác minh email mới" if pending else "Chưa đổi email; kiểm tra session, mailbox và Account UI"
        detail = _sanitize_exception_reason(
            exc,
            password=password,
            secret=secret,
            mailbox_key=mailbox_key,
        )
        if detail:
            message += ": " + detail
        log(f"[email] dừng ở stage={stage}: {detail}")
        raise EmailChangeError(
            message,
            pending=pending,
            definitive=getattr(exc, "definitive", False),
        ) from None
