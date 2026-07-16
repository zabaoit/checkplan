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
            await get_home(sess)
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
                raise ChangePasswordError(f"login failed: {exc}") from exc

            cookies = normalize_session_cookies(login_session.get("__cookies"))
            if not has_session_cookie(cookies):
                token = session_token_from_data(login_session)
                cookies = cookies_from_session_token(token) if token else []
            if not has_session_cookie(cookies):
                raise ChangePasswordError("login succeeded but session cookies are missing")
            set_cookies(sess, cookies)
            await get_home(sess)
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
        return await fetch_session(sess, email, log, proxy=proxy, label="refreshed")


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
    return str(payload.get("factor_id") or "")


async def get_home(sess: Any) -> None:
    await sess.get(
        "https://chatgpt.com/",
        headers=_chatgpt_headers(),
        allow_redirects=True,
        timeout=30.0,
    )


async def trigger_password_change_reauth(
    sess: Any,
    *,
    email: str,
    device_id: str,
    log: LogFn,
) -> str:
    log("[chpwd] triggering password-change reauth via API")
    csrf_resp = await sess.get(
        "https://chatgpt.com/api/auth/csrf",
        headers={**_chatgpt_headers(), "Accept": "application/json"},
        timeout=30.0,
    )
    if csrf_resp.status_code != 200:
        raise ChangePasswordError(f"Reauth csrf HTTP {csrf_resp.status_code}")
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
    resp = await sess.post(
        f"https://chatgpt.com/api/auth/signin/openai?{query}",
        headers={
            **_chatgpt_headers(),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://chatgpt.com",
        },
        data={
            "callbackUrl": "https://chatgpt.com/",
            "csrfToken": csrf_token,
            "json": "true",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise ChangePasswordError(
            f"Reauth NextAuth signin HTTP {resp.status_code}: {_safe_response_detail(resp)}"
        )
    redirect_url = str(response_json(resp).get("url") or "")
    if "auth.openai.com" not in redirect_url:
        raise ChangePasswordError("Reauth NextAuth signin did not return auth URL")
    log("[chpwd] reauth redirect URL ready")
    return redirect_url


async def open_password_page(sess: Any, authorize_url: str, log: LogFn) -> str:
    resp = await sess.get(
        authorize_url,
        headers=_chatgpt_headers(),
        allow_redirects=False,
        timeout=30.0,
    )
    location = str(resp.headers.get("Location") or resp.headers.get("location") or "")
    password_page_url = (
        urljoin(authorize_url, location)
        if location
        else "https://auth.openai.com/log-in/password"
    )
    final_resp = await sess.get(
        password_page_url,
        headers=_chatgpt_headers(referer=authorize_url),
        allow_redirects=True,
        timeout=30.0,
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
        raise ChangePasswordError(
            f"Current password verify HTTP {verify_resp.status_code}: "
            f"{_safe_response_detail(verify_resp)}"
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
        raise ChangePasswordError(
            f"Password reset HTTP {reset_resp.status_code}: {_safe_response_detail(reset_resp)}"
        )

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
) -> None:
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
            f"MFA issue HTTP {issue_resp.status_code}: {_safe_response_detail(issue_resp)}"
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
            f"MFA verify HTTP {verify_resp.status_code}: {_safe_response_detail(verify_resp)}"
        )
    callback_url = str(data.get("continue_url") or "")
    if "chatgpt.com/api/auth/callback/openai" not in callback_url:
        raise ChangePasswordError(f"MFA at {step} did not return callback URL")
    await nextauth_callback(sess, callback_url, log)


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
    log("[chpwd] NextAuth callback accepted")


async def fetch_session(
    sess: Any,
    email: str,
    log: LogFn,
    *,
    proxy: str | None,
    label: str,
) -> dict[str, Any]:
    resp = await sess.get(
        f"https://chatgpt.com/api/auth/session?chpwd={int(time.time() * 1000)}",
        headers={**_chatgpt_headers(), "Accept": "application/json"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise ChangePasswordError(
            f"{label} session HTTP {resp.status_code}: {_safe_response_detail(resp)}"
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
