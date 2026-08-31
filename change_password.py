"""Đổi mật khẩu ChatGPT bằng API-only flow.

Flow này tái sử dụng login/session hiện có của dự án, sau đó thực hiện reauth
``post_login_password_reset`` và gọi endpoint reset password của auth.openai.com.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlencode, urljoin

from curl_cffi.requests import AsyncSession

from session_phase import SessionError, fetch_account_entitlement, get_session_pure_request
from totp_helper import TotpError, generate_code, time_remaining
from user_agent_profile import (
    CURL_IMPERSONATE_PRIMARY,
    SEC_CH_UA,
    SEC_CH_UA_MOBILE,
    SEC_CH_UA_PLATFORM,
    WINDOWS_USER_AGENT,
)


LogFn = Callable[[str], None]
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
SESSION_COOKIE_CHUNK_SIZE = 3800
_ALLOWED_COOKIE_DOMAINS = ("chatgpt.com", "auth.openai.com")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")


class ChangePasswordError(Exception):
    """Đổi mật khẩu hoặc refresh session thất bại."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        committed: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.committed = committed


def _base_headers(*, referer: str, accept: str) -> dict[str, str]:
    return {
        "User-Agent": WINDOWS_USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
    }


def _chatgpt_headers(*, referer: str = "https://chatgpt.com/") -> dict[str, str]:
    return _base_headers(
        referer=referer,
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    )


def _auth_headers(*, referer: str) -> dict[str, str]:
    return _base_headers(referer=referer, accept="application/json")


def _safe_response_detail(resp: Any) -> str:
    text = _JWT_RE.sub("eyJ…[REDACTED]", str(getattr(resp, "text", "") or ""))
    return text[:160]


_TRANSIENT_HTTP_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_SESSION_REQUEST_ATTEMPTS = 3


def _is_transient_status(status: int) -> bool:
    return int(status) in _TRANSIENT_HTTP_STATUS


def _is_transient_error_text(message: str) -> bool:
    lowered = str(message or "").lower()
    if "invalid_auth_step" in lowered or "invalid_state" in lowered:
        return True
    return any(
        f"http {status}" in lowered
        for status in _TRANSIENT_HTTP_STATUS
    )


async def _get_with_warm_retry(
    sess: Any,
    url: str,
    *,
    headers: dict[str, str],
    log: LogFn,
    label: str,
    allow_redirects: bool = True,
    timeout: float = 30.0,
) -> Any:
    """GET có retry ngắn trên cùng cookie jar cho Cloudflare transient 403."""
    response = None
    for attempt in range(1, _SESSION_REQUEST_ATTEMPTS + 1):
        response = await sess.get(
            url,
            headers=headers,
            allow_redirects=allow_redirects,
            timeout=timeout,
        )
        if not _is_transient_status(response.status_code) or attempt >= _SESSION_REQUEST_ATTEMPTS:
            return response
        delay = float(attempt)
        log(
            f"[chpwd] {label} HTTP {response.status_code}; "
            f"warm-retry cùng session sau {delay:g}s "
            f"({attempt}/{_SESSION_REQUEST_ATTEMPTS})"
        )
        await asyncio.sleep(delay)
    return response


async def _post_with_warm_retry(
    sess: Any,
    url: str,
    *,
    headers: dict[str, str],
    log: LogFn,
    label: str,
    data: Any = None,
    json_body: Any = None,
    timeout: float = 30.0,
) -> Any:
    """POST bootstrap có retry 403; không dùng cho password/reset hoặc MFA verify."""
    response = None
    for attempt in range(1, _SESSION_REQUEST_ATTEMPTS + 1):
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
        }
        if data is not None:
            kwargs["data"] = data
        if json_body is not None:
            kwargs["json"] = json_body
        response = await sess.post(url, **kwargs)
        if response.status_code != 403 or attempt >= _SESSION_REQUEST_ATTEMPTS:
            return response
        delay = float(attempt)
        log(
            f"[chpwd] {label} HTTP 403; warm-retry cùng session sau {delay:g}s "
            f"({attempt}/{_SESSION_REQUEST_ATTEMPTS})"
        )
        await asyncio.sleep(delay)
    return response


async def change_password(
    *,
    email: str,
    current_password: str,
    new_password: str,
    check_plan_only: bool = False,
    skip_password_change: bool = False,
    secret: str | None = None,
    initial_session: str | dict[str, Any] | None = None,
    proxy: str | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Ổn định wrapper: retry toàn flow chỉ trước khi reset được commit."""
    last_error: ChangePasswordError | None = None
    for attempt in range(1, _SESSION_REQUEST_ATTEMPTS + 1):
        try:
            return await _change_password_once(
                email=email,
                current_password=current_password,
                new_password=new_password,
                check_plan_only=check_plan_only,
                skip_password_change=skip_password_change,
                secret=secret,
                initial_session=initial_session,
                proxy=proxy,
                log=log,
            )
        except ChangePasswordError as exc:
            last_error = exc
            if (
                attempt >= _SESSION_REQUEST_ATTEMPTS
                and not initial_session
                and not exc.committed
                and _is_transient_error_text(str(exc))
            ):
                log("[chpwd] pure-request exhausted; falling back to browser login")
                try:
                    from session_phase import get_session_browser

                    browser_session = await get_session_browser(
                        email=email,
                        password=current_password,
                        secret=secret,
                        headless=True,
                        proxy=proxy,
                        log=log,
                    )
                    return await _change_password_once(
                        email=email,
                        current_password=current_password,
                        new_password=new_password,
                        check_plan_only=check_plan_only,
                        skip_password_change=skip_password_change,
                        secret=secret,
                        initial_session=browser_session,
                        proxy=proxy,
                        log=log,
                    )
                except Exception as browser_exc:
                    raise ChangePasswordError(
                        f"{exc}; browser fallback failed: {browser_exc}"
                    ) from browser_exc
            if (
                not exc.retryable
                or exc.committed
                or attempt >= _SESSION_REQUEST_ATTEMPTS
            ):
                raise
            delay = float(attempt * 2)
            log(
                f"[chpwd] retry fresh flow sau {delay:g}s "
                f"({attempt}/{_SESSION_REQUEST_ATTEMPTS}): {exc}"
            )
            await asyncio.sleep(delay)
    if last_error is not None:
        raise last_error
    raise ChangePasswordError("change password failed without a result")


async def _change_password_once(
    *,
    email: str,
    current_password: str,
    new_password: str,
    check_plan_only: bool = False,
    skip_password_change: bool = False,
    secret: str | None = None,
    initial_session: str | dict[str, Any] | None = None,
    proxy: str | None = None,
    log: LogFn = print,
) -> dict[str, Any]:
    """Login/inject session → tùy chọn đổi password → trả session đã refresh."""
    email = str(email or "").strip()
    current_password = str(current_password or "")
    new_password = str(new_password or "")
    if not email or not current_password:
        raise ChangePasswordError("email/current_password are required")
    if not new_password and not check_plan_only and not skip_password_change:
        raise ChangePasswordError("new_password is required")

    proxies = {"http": proxy, "https": proxy} if proxy else None
    log("[chpwd] API-only flow starting")

    async with AsyncSession(
        impersonate=CURL_IMPERSONATE_PRIMARY,
        proxies=proxies,
    ) as sess:
        device_id = str(uuid.uuid4())

        if initial_session:
            cookies = cookies_from_initial_session(initial_session)
            set_cookies(sess, cookies)
            log(f"[chpwd] initial session provided; injecting {len(cookies)} cookie(s)")
            await get_home(sess, log)
            await fetch_session(sess, email, log, proxy=proxy, label="initial")
            log("[chpwd] initial session OK; skipped login")
        else:
            log("[chpwd] no initial session; using existing pure-request login")
            try:
                login_session = await get_session_pure_request(
                    email=email,
                    password=current_password,
                    secret=secret,
                    proxy=proxy,
                    log=log,
                )
            except SessionError as exc:
                detail = f"login failed: {exc}"
                raise ChangePasswordError(
                    detail,
                    retryable=_is_transient_error_text(detail),
                ) from exc

            cookies = normalize_session_cookies(login_session.get("__cookies"))
            if not has_session_cookie(cookies):
                token = session_token_from_data(login_session)
                cookies = cookies_from_session_token(token) if token else []
            if not has_session_cookie(cookies):
                raise ChangePasswordError("login succeeded but session cookies are missing")
            set_cookies(sess, cookies)
            await get_home(sess, log)
            await fetch_session(sess, email, log, proxy=proxy, label="login")
            log("[chpwd] login OK via existing session flow")

        if check_plan_only or skip_password_change:
            label = "plan-check" if check_plan_only else "no-change"
            log(f"[chpwd] {label} mode; skipping password change")
            return await fetch_session(sess, email, log, proxy=proxy, label=label)

        reauth_url = await trigger_password_change_reauth(
            sess,
            email=email,
            device_id=device_id,
            log=log,
        )
        await open_password_page(sess, reauth_url, log)
        await change_password_after_reauth(
            sess,
            current_password=current_password,
            new_password=new_password,
            secret=secret,
            log=log,
        )
        return await fetch_session(
            sess,
            email,
            log,
            proxy=proxy,
            label="refreshed",
            committed=True,
        )


def cookies_from_initial_session(
    initial_session: str | dict[str, Any],
) -> list[dict[str, Any]]:
    data: dict[str, Any] | None = None
    raw = ""
    if isinstance(initial_session, dict):
        data = initial_session
    else:
        raw = str(initial_session or "").strip()
        if not raw:
            raise ChangePasswordError("Initial session is empty")
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ChangePasswordError(f"Initial session JSON invalid: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ChangePasswordError("Initial session JSON must be an object")
            data = parsed

    if data is not None:
        cookies = normalize_session_cookies(data.get("__cookies") or data.get("cookies"))
        if has_session_cookie(cookies):
            return cookies
        token = session_token_from_data(data)
        if token:
            return cookies_from_session_token(token)
        raise ChangePasswordError("Initial session missing cookies/sessionToken")

    return cookies_from_session_token(raw)


def session_token_from_data(data: dict[str, Any]) -> str:
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    credentials = data.get("credentials") if isinstance(data.get("credentials"), dict) else {}
    token = (
        data.get("sessionToken")
        or data.get("session_token")
        or tokens.get("sessionToken")
        or tokens.get("session_token")
        or credentials.get("session_token")
    )
    return str(token or "").strip()


def cookies_from_session_token(token: str) -> list[dict[str, Any]]:
    token = str(token or "").strip()
    if not token:
        raise ChangePasswordError("Initial session token is empty")
    if len(token) <= SESSION_COOKIE_CHUNK_SIZE:
        chunks = [(SESSION_COOKIE_NAME, token)]
    else:
        chunks = [
            (f"{SESSION_COOKIE_NAME}.{idx}", token[pos:pos + SESSION_COOKIE_CHUNK_SIZE])
            for idx, pos in enumerate(range(0, len(token), SESSION_COOKIE_CHUNK_SIZE))
        ]
    return [
        {"name": name, "value": value, "domain": ".chatgpt.com", "path": "/"}
        for name, value in chunks
    ]


def normalize_session_cookies(cookies: Any) -> list[dict[str, Any]]:
    if not cookies:
        return []
    if isinstance(cookies, dict):
        iterable = [
            {"name": name, "value": value, "domain": ".chatgpt.com", "path": "/"}
            for name, value in cookies.items()
        ]
    elif isinstance(cookies, list):
        iterable = cookies
    else:
        return []

    out: list[dict[str, Any]] = []
    for cookie in iterable:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        domain = str(cookie.get("domain") or ".chatgpt.com")
        normalized_domain = domain.lstrip(".").lower()
        if not any(normalized_domain == allowed or normalized_domain.endswith(f".{allowed}")
                   for allowed in _ALLOWED_COOKIE_DOMAINS):
            continue
        out.append({
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure", True)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "sameSite": str(cookie.get("sameSite") or "Lax"),
            "expires": cookie.get("expires", -1),
        })
    return out


def has_session_cookie(cookies: list[dict[str, Any]]) -> bool:
    names = {cookie.get("name") for cookie in cookies}
    return SESSION_COOKIE_NAME in names or f"{SESSION_COOKIE_NAME}.0" in names


def set_cookies(sess: Any, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        sess.cookies.set(
            str(name),
            str(value),
            domain=str(cookie.get("domain") or ".chatgpt.com"),
            path=str(cookie.get("path") or "/"),
        )


def export_session_cookies(sess: Any) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    try:
        jar = sess.cookies.jar
    except Exception:
        return exported
    for cookie in jar:
        exported.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": cookie.name.startswith(("__Host-", "__Secure-")),
            "sameSite": "Lax",
            "expires": cookie.expires if cookie.expires else -1,
        })
    return normalize_session_cookies(exported)


def read_session_token(cookies: list[dict[str, Any]]) -> str:
    base = next(
        (str(c["value"]) for c in cookies if c.get("name") == SESSION_COOKIE_NAME),
        "",
    )
    if base:
        return base
    chunks: dict[int, str] = {}
    prefix = f"{SESSION_COOKIE_NAME}."
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if not name.startswith(prefix):
            continue
        try:
            chunks[int(name[len(prefix):])] = str(cookie.get("value") or "")
        except ValueError:
            continue
    return "".join(chunks[idx] for idx in sorted(chunks))


def get_cookie(sess: Any, name: str) -> str:
    try:
        for cookie in sess.cookies.jar:
            if cookie.name == name and cookie.value:
                return str(cookie.value)
    except Exception:
        pass
    return ""


def response_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def store_client_auth_session(sess: Any, data: dict[str, Any]) -> None:
    value = data.get("oai-client-auth-session")
    if isinstance(value, str) and value:
        sess.cookies.set(
            "oai-client-auth-session",
            value,
            domain="auth.openai.com",
            path="/",
        )


def factor_id(data: dict[str, Any]) -> str:
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    factors = payload.get("factors") if isinstance(payload.get("factors"), list) else []
    for factor in factors:
        if (
            isinstance(factor, dict)
            and factor.get("factor_type") == "totp"
            and factor.get("id")
        ):
            return str(factor["id"])
    payload_factor_id = str(payload.get("factor_id") or "")
    if payload_factor_id:
        return payload_factor_id
    continue_url = str(data.get("continue_url") or "")
    match = re.search(r"/mfa-challenge/([A-Za-z0-9_-]+)", continue_url)
    return match.group(1) if match else ""


async def get_home(sess: Any, log: LogFn = print) -> None:
    response = await _get_with_warm_retry(
        sess,
        "https://chatgpt.com/",
        headers=_chatgpt_headers(),
        log=log,
        label="home",
        allow_redirects=True,
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise ChangePasswordError(
            f"chatgpt home HTTP {response.status_code}",
            retryable=_is_transient_status(response.status_code),
        )


async def trigger_password_change_reauth(
    sess: Any,
    *,
    email: str,
    device_id: str,
    log: LogFn,
) -> str:
    log("[chpwd] triggering password-change reauth via API")
    csrf_resp = await _get_with_warm_retry(
        sess,
        "https://chatgpt.com/api/auth/csrf",
        headers={**_chatgpt_headers(), "Accept": "application/json"},
        log=log,
        label="reauth csrf",
        timeout=30.0,
    )
    if csrf_resp.status_code != 200:
        raise ChangePasswordError(
            f"Reauth csrf HTTP {csrf_resp.status_code}",
            retryable=_is_transient_status(csrf_resp.status_code),
        )
    csrf_token = str(response_json(csrf_resp).get("csrfToken") or "")
    if not csrf_token:
        raise ChangePasswordError("Reauth csrf token missing")

    oai_did = get_cookie(sess, "oai-did") or device_id
    query = urlencode({
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "post_login_password_reset": "true",
        "max_age": "0",
        "ext-oai-did": oai_did,
    })
    resp = await _post_with_warm_retry(
        sess,
        f"https://chatgpt.com/api/auth/signin/openai?{query}",
        headers={
            **_chatgpt_headers(),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://chatgpt.com",
        },
        log=log,
        label="reauth signin",
        data={
            "callbackUrl": "https://chatgpt.com/",
            "csrfToken": csrf_token,
            "json": "true",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise ChangePasswordError(
            f"Reauth NextAuth signin HTTP {resp.status_code}: {_safe_response_detail(resp)}",
            retryable=_is_transient_status(resp.status_code),
        )
    redirect_url = str(response_json(resp).get("url") or "")
    if "auth.openai.com" not in redirect_url:
        raise ChangePasswordError("Reauth NextAuth signin did not return auth URL")
    log("[chpwd] reauth redirect URL ready")
    return redirect_url


async def open_password_page(sess: Any, authorize_url: str, log: LogFn) -> str:
    resp = await _get_with_warm_retry(
        sess,
        authorize_url,
        headers=_chatgpt_headers(),
        log=log,
        label="reauth authorize",
        allow_redirects=False,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise ChangePasswordError(
            f"Reauth authorize HTTP {resp.status_code}: {_safe_response_detail(resp)}",
            retryable=_is_transient_status(resp.status_code),
        )
    location = str(resp.headers.get("Location") or resp.headers.get("location") or "")
    password_page_url = (
        urljoin(authorize_url, location)
        if location
        else "https://auth.openai.com/log-in/password"
    )
    final_resp = await _get_with_warm_retry(
        sess,
        password_page_url,
        headers=_chatgpt_headers(referer=authorize_url),
        log=log,
        label="password page",
        allow_redirects=True,
        timeout=30.0,
    )
    if final_resp.status_code >= 400:
        raise ChangePasswordError(
            f"Password page HTTP {final_resp.status_code}",
            retryable=_is_transient_status(final_resp.status_code),
        )
    final_url = str(getattr(final_resp, "url", "") or password_page_url)
    if "/log-in/password" not in password_page_url and "/log-in/password" not in final_url:
        raise ChangePasswordError(
            f"Expected /log-in/password, got: {final_url.split('?', 1)[0]}"
        )
    log(f"[chpwd] at: {final_url.split('?', 1)[0]}")
    return final_url


async def change_password_after_reauth(
    sess: Any,
    *,
    current_password: str,
    new_password: str,
    secret: str | None,
    log: LogFn,
) -> None:
    log("[chpwd] API reauth: submitting current password")
    verify_resp = await password_verify(
        sess,
        current_password,
        referer="https://auth.openai.com/log-in/password",
    )
    verify_data = response_json(verify_resp)
    store_client_auth_session(sess, verify_data)
    if verify_resp.status_code != 200:
        verify_detail = (
            f"Current password verify HTTP {verify_resp.status_code}: "
            f"{_safe_response_detail(verify_resp)}"
        )
        raise ChangePasswordError(
            verify_detail,
            retryable=_is_transient_error_text(verify_detail),
        )

    verify_page = verify_data.get("page") if isinstance(verify_data.get("page"), dict) else {}
    verify_page_type = str(verify_page.get("type") or "")
    verify_continue_url = str(verify_data.get("continue_url") or "")
    log(
        f"[chpwd] post-password → page_type={verify_page_type!r} "
        f"continue_url={verify_continue_url[:80]!r}"
    )

    # Account policy không đồng nhất: có account yêu cầu MFA trước khi state
    # machine cho phép POST password/reset, account khác nhận password mới trước
    # rồi mới challenge MFA. Phải drive đúng state trả về từ password/verify.
    requires_pre_reset_mfa = (
        "mfa" in verify_page_type.lower()
        or "/mfa-challenge/" in verify_continue_url.lower()
    )
    if requires_pre_reset_mfa:
        pre_reset_factor_id = factor_id(verify_data)
        if not pre_reset_factor_id:
            raise ChangePasswordError(
                "MFA required before password reset but challenge ID is missing"
            )
        await complete_mfa(
            sess,
            factor_id=pre_reset_factor_id,
            secret=secret,
            step="reauth",
            log=log,
            consume_callback=False,
        )

    log("[chpwd] API reset: submitting new password")
    reset_resp = await sess.post(
        "https://auth.openai.com/api/accounts/password/reset",
        headers={
            **_auth_headers(referer="https://auth.openai.com/reset-password/new-password"),
            "Content-Type": "application/json",
            "Origin": "https://auth.openai.com",
        },
        json={"password": new_password},
        timeout=30.0,
    )
    reset_data = response_json(reset_resp)
    store_client_auth_session(sess, reset_data)
    if reset_resp.status_code != 200:
        reset_body_lower = str(getattr(reset_resp, "text", "") or "").lower()
        reset_detail = (
            f"Password reset HTTP {reset_resp.status_code}: "
            f"{_safe_response_detail(reset_resp)}"
        )
        raise ChangePasswordError(
            reset_detail,
            # Reset là thao tác state-changing: chỉ retry khi server xác nhận
            # chưa vào đúng auth step (400/409), không retry mù HTTP 403/5xx
            # vì response có thể bị proxy/Cloudflare che sau khi đã commit.
            retryable=(
                "invalid_auth_step" in reset_body_lower
                or "invalid_state" in reset_body_lower
            ),
        )

    # Từ đây password/reset đã trả 200: không được retry toàn flow, tránh gửi
    # lại mật khẩu mới nếu chỉ callback/session refresh bị lỗi.
    try:
        callback_url = str(reset_data.get("continue_url") or "")
        if "chatgpt.com/api/auth/callback/openai" in callback_url:
            await nextauth_callback(sess, callback_url, log)
            log("[chpwd] password changed via API")
            return
        mfa_factor_id = factor_id(reset_data)
        if not mfa_factor_id:
            raise ChangePasswordError("Password reset did not return callback or MFA factor")
        await complete_mfa(
            sess,
            factor_id=mfa_factor_id,
            secret=secret,
            step="reset",
            log=log,
        )
        log("[chpwd] password changed via API")
    except ChangePasswordError as exc:
        if exc.committed:
            raise
        raise ChangePasswordError(
            str(exc),
            retryable=exc.retryable,
            committed=True,
        ) from exc


async def password_verify(sess: Any, password: str, *, referer: str) -> Any:
    return await sess.post(
        "https://auth.openai.com/api/accounts/password/verify",
        headers={
            **_auth_headers(referer=referer),
            "Content-Type": "application/json",
            "Origin": "https://auth.openai.com",
        },
        json={"password": password},
        timeout=30.0,
    )


async def complete_mfa(
    sess: Any,
    *,
    factor_id: str,
    secret: str | None,
    step: str,
    log: LogFn,
    consume_callback: bool = True,
) -> dict[str, Any]:
    if not secret:
        raise ChangePasswordError(f"2FA required at {step} but no secret provided")
    log(f"[chpwd] MFA at {step}; issuing TOTP challenge")
    referer = f"https://auth.openai.com/mfa-challenge/{factor_id}"
    issue_resp = await sess.post(
        "https://auth.openai.com/api/accounts/mfa/issue_challenge",
        headers={
            **_auth_headers(referer=referer),
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://auth.openai.com",
        },
        json={"id": factor_id, "type": "totp", "force_fresh_challenge": False},
        timeout=30.0,
    )
    if issue_resp.status_code != 200:
        raise ChangePasswordError(
            f"MFA issue HTTP {issue_resp.status_code}: {_safe_response_detail(issue_resp)}",
            retryable=_is_transient_status(issue_resp.status_code),
        )

    remaining = time_remaining()
    if remaining <= 5:
        wait_seconds = remaining + 0.5
        log(f"[chpwd] MFA at {step}; waiting {wait_seconds:.1f}s for fresh TOTP")
        await asyncio.sleep(wait_seconds)
    try:
        code = generate_code(secret)
    except TotpError as exc:
        raise ChangePasswordError(f"2FA secret invalid: {exc}") from exc
    log(f"[chpwd] MFA at {step}; submitting TOTP")
    verify_resp = await sess.post(
        "https://auth.openai.com/api/accounts/mfa/verify",
        headers={
            **_auth_headers(referer=referer),
            "Content-Type": "application/json",
            "Origin": "https://auth.openai.com",
        },
        json={"id": factor_id, "type": "totp", "code": code},
        timeout=30.0,
    )
    data = response_json(verify_resp)
    store_client_auth_session(sess, data)
    if verify_resp.status_code != 200:
        raise ChangePasswordError(
            f"MFA verify HTTP {verify_resp.status_code}: {_safe_response_detail(verify_resp)}",
            retryable=_is_transient_status(verify_resp.status_code),
        )
    log(f"[chpwd] MFA at {step}; verified")

    if not consume_callback:
        return data

    callback_url = str(data.get("continue_url") or "")
    if "chatgpt.com/api/auth/callback/openai" not in callback_url:
        raise ChangePasswordError(f"MFA at {step} did not return callback URL")
    await nextauth_callback(sess, callback_url, log)
    return data


async def nextauth_callback(sess: Any, callback_url: str, log: LogFn) -> None:
    resp = await sess.get(
        callback_url,
        headers=_chatgpt_headers(referer="https://auth.openai.com/"),
        allow_redirects=False,
        timeout=30.0,
    )
    if resp.status_code not in {302, 303}:
        raise ChangePasswordError(
            f"NextAuth callback HTTP {resp.status_code}: {_safe_response_detail(resp)}"
        )


def _inspect_backend_checkout_gpay(raw_data: dict[str, Any], log_fn: LogFn) -> str:
    """Inspect backend checkout response JSON for payment configuration & GPay support.

    Returns 3-state status:
        - "GPAY_SUPPORTED"
        - "GPAY_NOT_SUPPORTED"
        - "GPAY_UNKNOWN"
    """
    if not isinstance(raw_data, dict):
        log_fn("[checkout-debug] session created: false (raw_data invalid)")
        return "GPAY_UNKNOWN"

    try:
        session_id = str(raw_data.get("checkout_session_id") or raw_data.get("id") or "unknown")
        log_fn(f"[checkout-debug] session created: true")
        log_fn(f"[checkout-debug] session id: {session_id}")

        target_keys = {
            "payment_method_types",
            "payment_method_configuration",
            "payment_method_configuration_details",
            "payment_method_options",
            "payment_methods",
            "wallet",
            "google_pay",
            "googlepay",
        }

        found_paths: list[tuple[str, Any]] = []

        def _traverse(obj: Any, current_path: str):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    new_path = f"{current_path}.{k}" if current_path else k
                    if k.lower() in target_keys:
                        found_paths.append((new_path, v))
                    _traverse(v, new_path)
            elif isinstance(obj, list):
                for idx, item in enumerate(obj):
                    new_path = f"{current_path}[{idx}]"
                    _traverse(item, new_path)

        _traverse(raw_data, "")

        log_fn("[checkout-debug] payment-related paths:")
        if not found_paths:
            log_fn("  (none found)")
        else:
            for path, val in found_paths:
                log_fn(f"  {path} = {val}")

        pm_types = raw_data.get("payment_method_types")
        pm_config = raw_data.get("payment_method_configuration")
        pm_config_details = raw_data.get("payment_method_configuration_details")

        log_fn(f"[checkout-debug] payment_method_types: {pm_types}")
        log_fn(f"[checkout-debug] payment_method_configuration: {pm_config}")
        log_fn(f"[checkout-debug] payment_method_configuration_details: {pm_config_details}")

        # Check explicit payment_method_types array
        if isinstance(pm_types, list):
            pm_types_lower = [str(x).lower() for x in pm_types]
            if any(x in pm_types_lower for x in ("google_pay", "googlepay", "gpay")):
                log_fn("[checkout-debug] final gpay status: GPAY_SUPPORTED")
                return "GPAY_SUPPORTED"

        # Check explicit google_pay dict/bool in found paths
        for path, val in found_paths:
            key = path.split(".")[-1].lower()
            if key in ("google_pay", "googlepay", "gpay"):
                if val is True or (isinstance(val, str) and val.lower() in ("enabled", "auto", "active", "true")):
                    log_fn("[checkout-debug] final gpay status: GPAY_SUPPORTED")
                    return "GPAY_SUPPORTED"
                if val is False or (isinstance(val, str) and val.lower() in ("disabled", "false")):
                    log_fn("[checkout-debug] final gpay status: GPAY_NOT_SUPPORTED")
                    return "GPAY_NOT_SUPPORTED"

        log_fn("[checkout-debug] final gpay status: GPAY_UNKNOWN")
        return "GPAY_UNKNOWN"

    except Exception as exc:
        log_fn(f"[checkout-debug] inspection error: {exc}")
        log_fn("[checkout-debug] final gpay status: GPAY_UNKNOWN")
        return "GPAY_UNKNOWN"


def _check_gpay_in_stripe_data(stripe_data: dict[str, Any]) -> bool:
    if not isinstance(stripe_data, dict):
        return False
    return False


def _check_gpay_in_stripe_data(stripe_data: dict[str, Any]) -> bool:
    if not isinstance(stripe_data, dict):
        return False

    # 1. Check elements_options -> wallets / express_checkout
    elements_options = stripe_data.get("elements_options")
    if isinstance(elements_options, dict):
        wallets = elements_options.get("wallets")
        if isinstance(wallets, dict):
            gpay_val = wallets.get("google_pay") or wallets.get("googlePay")
            if isinstance(gpay_val, str) and gpay_val.lower() in ("auto", "always", "enabled", "on"):
                return True
            if isinstance(gpay_val, bool) and gpay_val:
                return True
            if isinstance(gpay_val, dict) and (gpay_val.get("enabled") is True or gpay_val.get("status") in ("active", "enabled")):
                return True

        express = elements_options.get("express_checkout") or elements_options.get("expressCheckout")
        if isinstance(express, dict):
            gpay_val = express.get("google_pay") or express.get("googlePay")
            if isinstance(gpay_val, str) and gpay_val.lower() in ("auto", "always", "enabled", "on"):
                return True
            if isinstance(gpay_val, bool) and gpay_val:
                return True

    # 2. Check top-level google_pay / google_pay_enabled
    if stripe_data.get("google_pay_enabled") is True:
        return True

    gpay_top = stripe_data.get("google_pay") or stripe_data.get("googlePay")
    if isinstance(gpay_top, dict):
        if gpay_top.get("enabled") is True or gpay_top.get("status") in ("active", "enabled"):
            return True
    elif isinstance(gpay_top, bool) and gpay_top:
        return True
    elif isinstance(gpay_top, str) and gpay_top.lower() in ("auto", "always", "enabled", "on"):
        return True

    # 3. Check payment_method_types / ordered_payment_method_types (ensuring not disabled)
    disabled_types = set()
    for d_key in ("disabled_payment_methods", "unsupported_payment_methods", "disabled_payment_method_types"):
        d_val = stripe_data.get(d_key)
        if isinstance(d_val, list):
            for x in d_val:
                disabled_types.add(str(x).lower())

    for key in ("payment_method_types", "ordered_payment_method_types"):
        pm_types = stripe_data.get(key)
        if isinstance(pm_types, list):
            for pm in pm_types:
                pm_str = str(pm).lower()
                if any(x in pm_str for x in ["google_pay", "gpay", "paywithgoogle", "googlepay"]):
                    if pm_str not in disabled_types:
                        return True

    return False


async def fetch_session(
    sess: Any,
    email: str,
    log: LogFn,
    *,
    proxy: str | None,
    label: str,
    committed: bool = False,
) -> dict[str, Any]:
    resp = await _get_with_warm_retry(
        sess,
        f"https://chatgpt.com/api/auth/session?chpwd={int(time.time() * 1000)}",
        headers={**_chatgpt_headers(), "Accept": "application/json"},
        log=log,
        label=f"{label} session",
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise ChangePasswordError(
            f"{label} session HTTP {resp.status_code}: {_safe_response_detail(resp)}",
            retryable=_is_transient_status(resp.status_code),
            committed=committed,
        )
    data = response_json(resp)
    access_token = str(data.get("accessToken") or "")
    if not access_token:
        raise ChangePasswordError(f"{label} session response missing accessToken")

    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    session_email = str(user.get("email") or "?")
    if email and session_email != "?" and session_email.lower() != email.lower():
        raise ChangePasswordError(f"{label} session email mismatch: {session_email}")

    token_ok, token_detail = await validate_access_token(access_token, proxy=proxy)
    if not token_ok:
        raise ChangePasswordError(f"{label} session accessToken invalid: {token_detail}")

    cookies = export_session_cookies(sess)
    try:
        entitlement = await fetch_account_entitlement(
            access_token=access_token,
            cookies=cookies,
            proxy=proxy,
        )
    except SessionError as exc:
        log(f"[chpwd] plan lookup failed: {exc}")
        entitlement = {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    plan = str(entitlement.get("plan") or account.get("planType") or "unknown")

    session_token = read_session_token(cookies)
    if session_token:
        data["sessionToken"] = session_token
    data["__cookies"] = cookies
    data["plan"] = plan

    if label == "plan-check":
        if plan.lower() == "plus":
            data["plan"] = "PLUS"
        else:
            log("[chpwd] checking GPT Plus offer & payment methods...")
            try:
                from payment_link import check_gpt_offer_async
                offer_res = await check_gpt_offer_async(
                    sess,
                    access_token,
                    promo_campaign="plus-1-month-free",
                    timeout=20.0,
                )
            except Exception as e:
                offer_res = {"success": False, "error": str(e)}

            if not offer_res.get("success") or not offer_res.get("has_trial"):
                data["plan"] = "KHÔNG CÓ OFFER"
                err_detail = offer_res.get("error") or "Không có offer 0đ"
                raise ChangePasswordError(f"KHÔNG CÓ OFFER: {err_detail}")

            pm_list = offer_res.get("payment_methods") or []
            pm_labels = []
            for pm in pm_list:
                pm_upper = pm.upper().replace("_", " ")
                if pm_upper in ("GOOGLE PAY", "GPAY"):
                    if "GPAY" not in pm_labels:
                        pm_labels.append("GPAY")
                elif pm_upper in ("APPLE PAY", "APPLEPAY"):
                    if "APPLE PAY" not in pm_labels:
                        pm_labels.append("APPLE PAY")
                elif pm_upper == "MOMO":
                    if "MOMO" not in pm_labels:
                        pm_labels.append("MOMO")

            methods_str = ", ".join(pm_labels)
            plan = f"CÓ OFFER ({methods_str})" if methods_str else "CÓ OFFER"
            data["plan"] = plan
            data["offer_details"] = offer_res

    log(
        f"[chpwd] session API OK ({label}): email={session_email}, "
        f"accessToken_len={len(access_token)}, sessionToken_len={len(session_token)}, "
        f"plan={plan}"
    )
    return data


async def validate_access_token(
    token: str,
    *,
    proxy: str | None = None,
) -> tuple[bool, str]:
    if not token:
        return False, "missing token"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        async with AsyncSession(
            impersonate=CURL_IMPERSONATE_PRIMARY,
            proxies=proxies,
        ) as sess:
            resp = await sess.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    **_chatgpt_headers(),
                    "Authorization": f"Bearer {token}",
                    "Accept": "*/*",
                    "Origin": "https://chatgpt.com",
                },
                timeout=12.0,
            )
    except Exception as exc:
        return False, f"validation network error: {exc}"
    if resp.status_code == 200:
        return True, "HTTP 200"
    return False, f"HTTP {resp.status_code}"
