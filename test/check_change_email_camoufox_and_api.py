"""Offline verification for Camoufox Account UI, CF cookie hygiene, fail-fast eligibility/begin errors, and API fallback."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import email_change
from email_change import (
    EmailChangeError,
    _change_in_browser,
    _change_via_api,
    _email_edit_control,
    _extract_auth0_client_id,
    _launch_email_browser_context,
    browser_cookies,
    change_email,
    session_cookies,
)
from playwright.async_api import async_playwright

SOURCE = "fixture@example.com"
TARGET = "fixture+z9k@gmail.com"
PASSWORD = "CorrectHorseBatteryStaple1!"
SECRET = "JBSWY3DPEHPK3PXP"
USER_ID = "user-abc123"
ACCOUNT_ID = "acc-xyz789"


def check_cookie_hygiene_and_bootstrap() -> None:
    session = {
        "user": {"email": SOURCE, "id": USER_ID},
        "__cookies": [
            {"name": "__Secure-next-auth.session-token.0", "value": "tok0", "domain": ".chatgpt.com"},
            {"name": "__Secure-next-auth.session-token.1", "value": "tok1", "domain": ".chatgpt.com"},
            {"name": "oai-did", "value": "did-123", "domain": ".chatgpt.com"},
            {"name": "__cf_bm", "value": "curl-cffi-bm", "domain": ".chatgpt.com"},
            {"name": "_cfuvid", "value": "curl-cffi-uvid", "domain": ".chatgpt.com"},
            {"name": "cf_clearance", "value": "curl-cffi-clr", "domain": ".chatgpt.com"},
        ],
    }
    full = session_cookies(session)
    assert len(full) == 6
    clean = browser_cookies(session)
    clean_names = {c["name"] for c in clean}
    assert clean_names == {
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
        "oai-did",
    }, clean_names

    html = (
        '<html><head><script id="client-bootstrap" type="application/json">'
        '{"auth":{"auth0ClientId":"CustomAuth0Client123","reauthenticationParams":{"reauth":"password"}}}'
        "</script></head></html>"
    )
    assert _extract_auth0_client_id(html) == "CustomAuth0Client123"
    assert _extract_auth0_client_id("<html></html>") == "TdJIcbe16WoTHtN95nyywh5E4yOo6ItG"


async def check_camoufox_launcher_selection() -> None:
    captured: dict[str, object] = {}

    class FakeCamoufox:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.closed = False

        async def __aenter__(self):
            captured["entered"] = True
            return SimpleNamespace(pages=[])

        async def __aexit__(self, *args):
            captured["exited"] = True

    fake_camoufox_mod = types.ModuleType("camoufox.async_api")
    fake_camoufox_mod.AsyncCamoufox = FakeCamoufox  # type: ignore[attr-defined]

    with tempfile.TemporaryDirectory(prefix="cf_launch_", dir=ROOT / "test") as tmp:
        settings = SimpleNamespace(
            browser_engine="camoufox",
            browser_headless=True,
            runtime_dir=Path(tmp),
        )
        proxy = {"server": "http://127.0.0.1:8888"}
        with patch.dict(sys.modules, {"camoufox.async_api": fake_camoufox_mod}):
            async with _launch_email_browser_context(settings, Path(tmp) / "prof", proxy, lambda _: None) as ctx:
                assert ctx is not None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["persistent_context"] is True
    assert kwargs["headless"] is True
    assert kwargs["os"] == ["windows"]
    assert kwargs["geoip"] is True
    assert kwargs["proxy"] == proxy
    assert captured.get("entered") is True and captured.get("exited") is True


async def check_production_dom_and_fail_fast_browser() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        try:
            # 1. Name == Email on page + unique aria-label="Change email: {email}" must succeed.
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            icon = '<svg width="16" height="16"><path d="M1 1L15 15"/></svg>'
            await page.set_content(
                f"""
                <div><span>Name</span><span>{SOURCE}</span><button aria-label="Edit name">{icon}</button></div>
                <div>
                  <div><span>Email</span><span class="text-sm text-secondary select-text">{SOURCE}</span></div>
                  <button type="button" aria-label="Change email: {SOURCE}" aria-haspopup="dialog"
                          onclick="document.getElementById('dlg').showModal()">{icon}</button>
                </div>
                <dialog id="dlg"><input type="email" name="email"></dialog>
                """
            )
            ctrl = await _email_edit_control(page, SOURCE)
            await ctrl.click()
            await page.locator('input[name="email"]:visible').wait_for(state="visible", timeout=1000)
            await page.close()

            # 2. Full production EmailDialog simulation:
            #    - Verify button disabled until 6 digits entered
            #    - Toast says "Email confirmed" (exact ChatGPT bundle text)
            #    - Scenarios for eligible=false and /begin 400 email_change_account_too_new
            prod_html = f"""
            <!doctype html><html><body>
              <div id="account-root">
                <div><span>Name</span><span>{SOURCE}</span><button aria-label="Edit name">{icon}</button></div>
                <div id="email-row">
                  <div><span>Email</span><span>{SOURCE}</span></div>
                  <span id="pencil-slot"></span>
                </div>
              </div>
              <dialog id="email-dlg">
                <form id="email-form">
                  <h2 id="dlg-title">Edit email</h2>
                  <div id="step-email">
                    <label><span>Email address</span><input name="email" type="email" autocomplete="email"></label>
                  </div>
                  <div id="step-otp" style="display:none">
                    <label><span>Verification code</span>
                      <input name="verification_code" inputmode="numeric" autocomplete="one-time-code" maxlength="6">
                    </label>
                  </div>
                  <div id="alert-box" role="alert" style="display:none"></div>
                  <button type="submit" id="submit-btn">Send verification email</button>
                </form>
              </dialog>
              <script>
                let step = 'email';
                fetch('/backend-api/accounts/change_email/eligibility').then(async r => {{
                  if (!r.ok) return;
                  const data = await r.json();
                  if (data.eligible === true) {{
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.setAttribute('aria-label', 'Change email: {SOURCE}');
                    btn.innerHTML = '{icon}';
                    btn.onclick = () => {{
                      if (data.eligibility_type === 'social') {{
                        document.body.insertAdjacentHTML('beforeend', '<dialog open><h2>Add a password first</h2></dialog>');
                        return;
                      }}
                      document.getElementById('email-dlg').showModal();
                    }};
                    document.getElementById('pencil-slot').appendChild(btn);
                  }}
                }});
                const otpInput = document.querySelector('input[name="verification_code"]');
                const submitBtn = document.getElementById('submit-btn');
                otpInput.addEventListener('input', () => {{
                  otpInput.value = otpInput.value.replace(/\\D/g, '').slice(0, 6);
                  submitBtn.disabled = (step === 'otp' && otpInput.value.length !== 6);
                }});
                document.getElementById('email-form').addEventListener('submit', async (e) => {{
                  e.preventDefault();
                  const emailVal = document.querySelector('input[name="email"]').value;
                  if (step === 'email') {{
                    const r = await fetch('/backend-api/accounts/change_email/begin', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{email: emailVal, auth0_client_id: 'TdJIcbe16WoTHtN95nyywh5E4yOo6ItG'}})
                    }});
                    if (!r.ok) {{
                      const err = await r.json();
                      const alertBox = document.getElementById('alert-box');
                      alertBox.textContent = err.detail?.message || 'Error';
                      alertBox.style.display = 'block';
                      return;
                    }}
                    step = 'otp';
                    document.getElementById('step-email').style.display = 'none';
                    document.getElementById('step-otp').style.display = 'block';
                    submitBtn.textContent = 'Verify';
                    submitBtn.disabled = true;
                  }} else {{
                    const r = await fetch('/backend-api/accounts/change_email/verify', {{
                      method: 'POST',
                      headers: {{'Content-Type': 'application/json'}},
                      body: JSON.stringify({{email: emailVal, code: otpInput.value}})
                    }});
                    if (r.ok) {{
                      document.getElementById('email-dlg').close();
                      document.body.insertAdjacentHTML('beforeend', '<div role="status">Email confirmed</div>');
                    }}
                  }}
                }});
              </script>
            </body></html>
            """

            mode = {"kind": "ok"}
            events: list[str] = []
            poll_called = {"count": 0}
            injected_cookie_names: list[str] = []

            async def route_handler(route):
                path = urlsplit(route.request.url).path
                if path == "/settings/account":
                    await route.fulfill(status=200, content_type="text/html", body=prod_html)
                elif path == "/api/auth/session":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"user": {"email": SOURCE, "id": USER_ID}, "accessToken": "tok"}),
                    )
                elif path == "/backend-api/accounts/change_email/eligibility":
                    if mode["kind"] == "ineligible":
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"eligible": False, "reason": "email_change_account_too_new"}),
                        )
                    elif mode["kind"] == "social":
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"eligible": True, "eligibility_type": "social"}),
                        )
                    else:
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"eligible": True, "eligibility_type": "password"}),
                        )
                elif path == "/backend-api/accounts/change_email/begin":
                    if mode["kind"] == "too_new_begin":
                        await route.fulfill(
                            status=400,
                            content_type="application/json",
                            body=json.dumps({
                                "detail": {
                                    "code": "email_change_account_too_new",
                                    "message": "You can change your email address 24 hours after creating your account.",
                                }
                            }),
                        )
                    else:
                        await route.fulfill(status=200, content_type="application/json", body="{}")
                elif path == "/backend-api/accounts/change_email/verify":
                    payload = json.loads(route.request.post_data or "{}")
                    assert payload == {"email": TARGET, "code": "654321"}, payload
                    await route.fulfill(status=200, content_type="application/json", body="{}")
                else:
                    await route.abort()

            async def launch(**options):
                context = await browser.new_context()
                orig_add_cookies = context.add_cookies

                async def capture_cookies(cookies):
                    injected_cookie_names[:] = [c["name"] for c in cookies]
                    await orig_add_cookies(cookies)

                context.add_cookies = capture_cookies  # type: ignore[method-assign]
                await context.route("**/*", route_handler)
                await context.new_page()
                return context

            @asynccontextmanager
            async def offline_pw():
                yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

            async def save(stage: str, uid: str) -> None:
                assert uid == USER_ID
                events.append(stage)

            async def prepare() -> None:
                events.append("prepare")

            async def poll_otp(**kwargs) -> str:
                poll_called["count"] += 1
                assert events == ["prepared", "prepare", "submitted"], events
                return "654321"

            raw_session = {
                "user": {"email": SOURCE, "id": USER_ID},
                "__mfa_verified": True,
                "__cookies": [
                    {"name": "__Secure-next-auth.session-token", "value": "sess-cookie"},
                    {
                        "name": "__Host-next-auth.csrf-token",
                        "value": "csrf-cookie",
                        "domain": "chatgpt.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": -1,
                    },
                    {"name": "__cf_bm", "value": "stale-cf-bm"},
                ],
            }

            async def run_once():
                with tempfile.TemporaryDirectory(prefix="prod_dom_", dir=ROOT / "test") as prof:
                    settings = SimpleNamespace(browser_headless=True, profiles_dir=Path(prof))
                    with patch("playwright.async_api.async_playwright", offline_pw), patch.dict(
                        sys.modules,
                        {
                            "config": SimpleNamespace(load_settings=lambda: settings),
                            "browser_momo": SimpleNamespace(_parse_playwright_proxy=lambda _: None),
                        },
                    ):
                        await _change_in_browser(
                            email=SOURCE,
                            target_email=TARGET,
                            session=raw_session,
                            proxy=None,
                            mailbox=SimpleNamespace(prepare=prepare, poll_otp=poll_otp),
                            save=save,
                            password=PASSWORD,
                            secret=SECRET,
                        )

            # Case A: Happy path with disabled-until-6-digits Verify + "Email confirmed"
            await run_once()
            assert injected_cookie_names == [
                "__Secure-next-auth.session-token",
                "__Host-next-auth.csrf-token",
            ], injected_cookie_names
            assert events == ["prepared", "prepare", "submitted", "changed_pending_verify"], events
            assert poll_called["count"] == 1

            # Case B: /begin 400 email_change_account_too_new -> MUST NOT save("submitted") or poll Congas!
            events.clear()
            poll_called["count"] = 0
            mode["kind"] = "too_new_begin"
            try:
                await run_once()
            except EmailChangeError as exc:
                assert exc.definitive is True
                assert "24 giờ" in str(exc) or "24 hours" in str(exc), str(exc)
            else:
                raise AssertionError("begin 400 must fail fast")
            assert events == ["prepared", "prepare"], events
            assert poll_called["count"] == 0

            # Case C: eligibility_type="social" -> definitive error before mailbox prepare
            events.clear()
            mode["kind"] = "social"
            try:
                await run_once()
            except EmailChangeError as exc:
                assert exc.definitive is True
                assert "mật khẩu" in str(exc), str(exc)
            else:
                raise AssertionError("social account without password must fail fast")
            assert events == ["prepared"], events

            # Case D: eligible=false -> definitive error explaining ineligibility
            events.clear()
            mode["kind"] = "ineligible"
            try:
                await run_once()
            except EmailChangeError as exc:
                assert exc.definitive is True
                assert "email_change_account_too_new" in str(exc), str(exc)
            else:
                raise AssertionError("ineligible account must fail fast")
            assert events == ["prepared"], events
        finally:
            await browser.close()


async def check_api_fallback_with_reauth() -> None:
    calls: list[tuple[str, str, object]] = []
    begin_attempts = {"count": 0}
    reauth_done = {"ok": False}

    class FakeResp:
        def __init__(self, status_code: int, payload: object = None, text: str = "", headers: dict | None = None):
            self.status_code = status_code
            self._payload = payload
            self.text = text if text else (json.dumps(payload) if payload is not None else "")
            self.headers = headers or {}
            self.url = ""

        def json(self):
            if isinstance(self._payload, dict):
                return self._payload
            return json.loads(self.text)

    class FakeJarCookie:
        def __init__(self, name: str, value: str, domain: str = ".chatgpt.com", path: str = "/"):
            self.name = name
            self.value = value
            self.domain = domain
            self.path = path
            self.secure = True
            self.expires = None

    class FakeCookies:
        def __init__(self):
            self.jar: list[FakeJarCookie] = []

        def set(self, name: str, value: str, domain: str = ".chatgpt.com", path: str = "/"):
            self.jar = [c for c in self.jar if not (c.name == name and c.domain == domain)]
            self.jar.append(FakeJarCookie(name, value, domain, path))

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = FakeCookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str, **kwargs):
            parsed = urlsplit(url)
            calls.append(("GET", parsed.path, None))
            if parsed.path == "/settings/account":
                return FakeResp(
                    200,
                    text='<script id="client-bootstrap" type="application/json">{"auth":{"auth0ClientId":"LiveClient99"}}</script>',
                )
            if parsed.path == "/api/auth/session":
                tok = "fresh-reauth-token" if reauth_done["ok"] else "initial-token"
                return FakeResp(
                    200,
                    payload={
                        "user": {"email": SOURCE, "id": USER_ID},
                        "accessToken": tok,
                        "account": {"id": ACCOUNT_ID},
                    },
                )
            if parsed.path == "/backend-api/accounts/change_email/eligibility":
                return FakeResp(200, payload={"eligible": True, "eligibility_type": "social_password"})
            if parsed.path == "/api/auth/csrf":
                return FakeResp(200, payload={"csrfToken": "csrf-xyz"})
            if parsed.hostname == "auth.openai.com" and parsed.path == "/authorize":
                r = FakeResp(302, headers={"Location": "https://auth.openai.com/log-in/password"})
                r.url = url
                return r
            if parsed.hostname == "auth.openai.com" and parsed.path == "/log-in/password":
                r = FakeResp(200, text="<html>password</html>")
                r.url = url
                return r
            if parsed.path == "/api/auth/callback/openai":
                reauth_done["ok"] = True
                return FakeResp(302, headers={"Location": "https://chatgpt.com/settings/account"})
            raise AssertionError(f"Unexpected GET {url}")

        async def post(self, url: str, **kwargs):
            parsed = urlsplit(url)
            body = kwargs.get("json") if "json" in kwargs else kwargs.get("data")
            calls.append(("POST", parsed.path, body))
            if parsed.path == "/backend-api/accounts/change_email/begin":
                begin_attempts["count"] += 1
                assert body == {
                    "email": TARGET,
                    "auth0_client_id": "LiveClient99",
                    "remove_social_subs": True,
                }, body
                if begin_attempts["count"] == 1:
                    return FakeResp(401, payload={"detail": {"code": "reauth_required", "message": "Reauth required"}})
                assert kwargs["headers"]["Authorization"] == "Bearer fresh-reauth-token"
                return FakeResp(200, payload={})
            if parsed.path == "/api/auth/signin/openai":
                qs = parse_qs(parsed.query)
                assert qs.get("reauth") == ["password"] and qs.get("login_hint") == [SOURCE], qs
                return FakeResp(200, payload={"url": "https://auth.openai.com/authorize?state=1"})
            if parsed.hostname == "auth.openai.com" and parsed.path == "/api/accounts/password/verify":
                assert body == {"password": PASSWORD}
                return FakeResp(
                    200,
                    payload={
                        "page": {"type": "mfa_challenge", "payload": {"factors": [{"id": "fac-1", "factor_type": "totp"}]}},
                        "continue_url": "https://auth.openai.com/mfa-challenge/fac-1",
                    },
                )
            if parsed.hostname == "auth.openai.com" and parsed.path == "/api/accounts/mfa/issue_challenge":
                return FakeResp(200, payload={})
            if parsed.hostname == "auth.openai.com" and parsed.path == "/api/accounts/mfa/verify":
                assert body["id"] == "fac-1" and len(body["code"]) == 6
                return FakeResp(200, payload={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=ok"})
            if parsed.path == "/backend-api/accounts/change_email/verify":
                assert body == {"email": TARGET, "code": "778899", "remove_social_subs": True}, body
                return FakeResp(200, payload={})
            raise AssertionError(f"Unexpected POST {url}")

    class FakeMailbox:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prepare(self):
            calls.append(("MAIL", "prepare", None))

        async def poll_otp(self, **kwargs):
            calls.append(("MAIL", "poll_otp", kwargs["recipient"]))
            return "778899"

    source_session = {
        "user": {"email": SOURCE, "id": USER_ID},
        "accessToken": "initial-token",
        "__mfa_verified": True,
        "__cookies": [
            {"name": "__Secure-next-auth.session-token", "value": "cookie-val", "domain": ".chatgpt.com"},
            {"name": "oai-did", "value": "did-fixed", "domain": ".chatgpt.com"},
        ],
    }
    fresh_session = {
        "user": {"email": TARGET, "id": USER_ID},
        "__mfa_verified": True,
    }
    login_mock = AsyncMock(side_effect=[source_session, fresh_session])
    browser_cf_fail = AsyncMock(
        side_effect=EmailChangeError(
            "Cloudflare chặn browser tự động khi kiểm tra quyền đổi email (HTTP 403); chưa gửi yêu cầu đổi email",
            definitive=False,
        )
    )
    checkpoints: list[tuple[str, str]] = []
    logs: list[str] = []

    async def checkpoint(stage: str, uid: str) -> None:
        checkpoints.append((stage, uid))

    with patch("curl_cffi.requests.AsyncSession", FakeAsyncSession), patch.dict(
        sys.modules,
        {
            "session_phase": SimpleNamespace(
                get_session_pure_request=login_mock,
                SessionError=RuntimeError,
                fetch_account_entitlement=AsyncMock(),
            ),
            "congas_mail": SimpleNamespace(CongasMailbox=FakeMailbox),
        },
    ), patch.object(email_change, "_change_in_browser", browser_cf_fail):
        result = await change_email(
            email=SOURCE,
            password=PASSWORD,
            secret=SECRET,
            target_email=TARGET,
            mailbox_email="fixture@gmail.com",
            mailbox_key="ABCD1234EFGH",
            checkpoint=checkpoint,
            log=logs.append,
        )

    assert result == {
        "email": TARGET,
        "secret": SECRET,
        "login_verified": True,
        "user_id": USER_ID,
    }
    assert checkpoints == [
        ("prepared", USER_ID),
        ("submitted", USER_ID),
        ("changed_pending_verify", USER_ID),
        ("verified", USER_ID),
    ], checkpoints
    assert begin_attempts["count"] == 2
    assert any("chuyển sang API đổi email trực tiếp" in line for line in logs), logs

    # Definitive browser rejection (e.g. account <24h old) MUST NOT trigger _change_via_api!
    definitive_browser_fail = AsyncMock(
        side_effect=EmailChangeError(
            "Tài khoản mới tạo chưa đủ 24 giờ để đổi email (email_change_account_too_new)",
            definitive=True,
        )
    )
    api_spy = AsyncMock()
    login_once = AsyncMock(return_value=source_session)
    checkpoints2: list[tuple[str, str]] = []

    with patch.dict(
        sys.modules,
        {
            "session_phase": SimpleNamespace(get_session_pure_request=login_once),
            "congas_mail": SimpleNamespace(CongasMailbox=FakeMailbox),
        },
    ), patch.object(email_change, "_change_in_browser", definitive_browser_fail), patch.object(
        email_change, "_change_via_api", api_spy
    ):
        try:
            await change_email(
                email=SOURCE,
                password=PASSWORD,
                secret=SECRET,
                target_email=TARGET,
                mailbox_email="fixture@gmail.com",
                mailbox_key="ABCD1234EFGH",
                checkpoint=lambda s, u: _append_async(checkpoints2, (s, u)),
            )
        except EmailChangeError as exc:
            assert exc.error_kind == "email_change_failed"
            assert exc.definitive is True
            assert "email_change_account_too_new" in str(exc)
        else:
            raise AssertionError("Definitive rejection must raise EmailChangeError")
    api_spy.assert_not_awaited()
    assert checkpoints2 == [("prepared", USER_ID)], checkpoints2


def check_congas_mime_clockskew_and_templates() -> None:
    import base64
    from datetime import datetime, timedelta, timezone
    from congas_mail import extract_otp

    now = datetime.now(timezone.utc)
    alias = "fixture+abc@gmail.com"
    mime_subj = "=?UTF-8?B?" + base64.b64encode(b"Your ChatGPT code is 482910").decode("ascii") + "?="
    # 1. RFC-2047 MIME subject + 15s negative clock skew + 6-digit Help Center URL + base mailbox in 'to'
    msg = {
        "id": "mime-fresh-1",
        "receivedAt": (now.replace(microsecond=0) - timedelta(seconds=15)).isoformat(),
        "from": "ChatGPT <noreply@tm.openai.com>",
        "to": "fixture@gmail.com",
        "subject": mime_subj,
        "htmlBody": (
            "<p>Enter this temporary verification code to continue: <b>482910</b></p>"
            "<p>Help: https://help.openai.com/en/articles/493682-verify</p>"
        ),
    }
    assert extract_otp(msg, alias, now, set()) == "482910"
    # Baseline ID still strictly rejected even within clock-skew grace
    assert extract_otp(msg, alias, now, {"mime-fresh-1"}) == ""
    # Different +alias in 'to' still strictly rejected
    assert extract_otp({**msg, "to": "fixture+xyz@gmail.com"}, alias, now, set()) == ""
    # Older than 30s grace still strictly rejected
    assert extract_otp(
        {**msg, "receivedAt": (now.replace(microsecond=0) - timedelta(seconds=45)).isoformat()},
        alias,
        now,
        set(),
    ) == ""

    # 2. Real Auth0 / OpenAI email templates that previously failed [^0-9]{0,160}, brand, or naive timestamp:
    # 2a. Digits in "10 minutes" + digits in target alias rd2508237+wq6@gmail.com + OpenAI brand ONLY in <img alt="OpenAI"> + logo@2x.png
    real_alias = "rd2508237+wq6@gmail.com"
    naive_ts = now.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    auth0_html_msg = {
        "id": "congas-live-auth0-1",
        "receivedAt": naive_ts,
        "subject": "Verify your email address",
        "htmlBody": (
            '<div><img alt="OpenAI" src="https://cdn.openai.com/assets/logo@2x.png" />'
            "<p>Verify your email address for rd2508237+wq6@gmail.com.</p>"
            "<p>Enter the following 6-digit verification code. This code will expire in 10 minutes:</p>"
            '<div style="font-size:32px;font-weight:bold">839204</div>'
            "<p>If you didn't request this email, safely ignore it.</p></div>"
        ),
    }
    assert extract_otp(auth0_html_msg, real_alias, now, set()) == "839204"

    # 2b. Hero OTP placed BEFORE "This code expires in 15 minutes" + old source_email mentioned in body
    old_source = "yardarm_boll_0a+37island.maple@icloud.com"
    hero_first_msg = {
        "id": "congas-hero-first-2",
        "date": "Mon, 02 Nov 2026 07:45:05 -0000" if now.year == 2099 else now.strftime("%a, %d %b %Y %H:%M:%S -0000"),
        "subject": "ChatGPT",
        "htmlBody": (
            f"<p>Hi {old_source},</p>"
            "<h1>517392</h1>"
            "<p>Use this verification code within 15 minutes to update your ChatGPT account email.</p>"
        ),
    }
    assert extract_otp(hero_first_msg, real_alias, now, set(), source_email=old_source) == "517392"
    # Unrelated foreign email in body is still rejected even when source_email is passed
    foreign_msg = {
        **hero_first_msg,
        "id": "congas-foreign-3",
        "htmlBody": hero_first_msg["htmlBody"].replace(old_source, "stranger_99@icloud.com"),
    }
    assert extract_otp(foreign_msg, real_alias, now, set(), source_email=old_source) == ""

    # 2c. Vietnamese template
    vn_msg = {
        "id": "congas-vn-4",
        "receivedAt": now.isoformat(),
        "subject": "Mã xác minh ChatGPT",
        "htmlBody": "<p>Mã xác minh của bạn là <b>604128</b>. Mã có hiệu lực trong 10 phút.</p>",
    }
    assert extract_otp(vn_msg, real_alias, now, set()) == "604128"

    # 2d. Live OpenAI Auth0 email-change application name "Dharma Mail verification code"
    # (no "OpenAI"/"ChatGPT" string anywhere in subject or HTML; Congas omits From header)
    dharma_msg = {
        "id": "congas-dharma-5",
        "receivedAt": now.isoformat(),
        "subject": "Dharma Mail verification code",
        "htmlBody": (
            "<div><h2>Dharma Mail</h2>"
            "<p>Your Dharma Mail verification code is:</p>"
            '<p style="font-size:28px;font-weight:700">729415</p>'
            "<p>This code expires in 10 minutes.</p></div>"
        ),
    }
    assert extract_otp(dharma_msg, real_alias, now, set()) == "729415"

    # Minimal Dharma Mail body (subject carries 'verification code', body only renders 6 digits or spaced spans)
    dharma_spaced = {
        "id": "congas-dharma-6",
        "receivedAt": now.isoformat(),
        "subject": "Dharma Mail verification code",
        "htmlBody": "<div><span>3</span><span>9</span><span>1</span><span>8</span><span>4</span><span>6</span></div>",
    }
    assert extract_otp(dharma_spaced, real_alias, now, set()) == "391846"


async def check_api_otp_timeout_stays_prepared() -> None:
    class FakeResp:
        def __init__(self, status_code: int, payload: object = None, text: str = ""):
            self.status_code = status_code
            self._payload = payload
            self.text = text if text else (json.dumps(payload) if payload is not None else "")
            self.headers: dict[str, str] = {}

        def json(self):
            return self._payload if isinstance(self._payload, dict) else json.loads(self.text)

    class FakeJarCookie:
        def __init__(self, name: str, value: str, domain: str = ".chatgpt.com", path: str = "/"):
            self.name = name
            self.value = value
            self.domain = domain
            self.path = path
            self.secure = True
            self.expires = None

    class FakeCookies:
        def __init__(self):
            self.jar: list[FakeJarCookie] = []

        def set(self, name: str, value: str, domain: str = ".chatgpt.com", path: str = "/"):
            self.jar = [c for c in self.jar if not (c.name == name and c.domain == domain)]
            self.jar.append(FakeJarCookie(name, value, domain, path))

    class FakeAsyncSession:
        def __init__(self, **kwargs):
            self.cookies = FakeCookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str, **kwargs):
            path = urlsplit(url).path
            if path == "/settings/account":
                return FakeResp(200, text="<html></html>")
            if path == "/api/auth/session":
                return FakeResp(
                    200,
                    payload={"user": {"email": SOURCE, "id": USER_ID}, "accessToken": "tok-1"},
                )
            if path == "/backend-api/accounts/change_email/eligibility":
                return FakeResp(200, payload={"eligible": True, "eligibility_type": "password"})
            raise AssertionError(f"Unexpected GET {url}")

        async def post(self, url: str, **kwargs):
            path = urlsplit(url).path
            if path == "/backend-api/accounts/change_email/begin":
                return FakeResp(200, payload={})
            raise AssertionError(f"Unexpected POST {url}")

    class TimeoutMailbox:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prepare(self):
            pass

        async def poll_otp(self, **kwargs):
            assert kwargs.get("source_email") == SOURCE
            raise TimeoutError()

    source_session = {
        "user": {"email": SOURCE, "id": USER_ID},
        "accessToken": "tok-1",
        "__mfa_verified": True,
        "__cookies": [{"name": "__Secure-next-auth.session-token", "value": "v", "domain": ".chatgpt.com"}],
    }
    class DummyCongasMailError(Exception):
        pass

    stages: list[tuple[str, str]] = []
    logs: list[str] = []
    browser_spy = AsyncMock()
    orig_default = email_change._DEFAULT_CHANGE_IN_BROWSER
    with patch("curl_cffi.requests.AsyncSession", FakeAsyncSession), patch.dict(
        sys.modules,
        {
            "session_phase": SimpleNamespace(
                get_session_pure_request=AsyncMock(return_value=source_session),
                SessionError=RuntimeError,
                fetch_account_entitlement=AsyncMock(),
            ),
            "congas_mail": SimpleNamespace(CongasMailbox=TimeoutMailbox, CongasMailError=DummyCongasMailError),
        },
    ):
        try:
            email_change._change_in_browser = browser_spy  # type: ignore[assignment]
            email_change._DEFAULT_CHANGE_IN_BROWSER = browser_spy  # type: ignore[assignment]
            try:
                await change_email(
                    email=SOURCE,
                    password=PASSWORD,
                    secret=SECRET,
                    target_email=TARGET,
                    mailbox_email="fixture@gmail.com",
                    mailbox_key="ABCD1234EFGH",
                    checkpoint=lambda s, u: _append_async(stages, (s, u)),
                    log=logs.append,
                )
            except EmailChangeError as exc:
                assert exc.error_kind == "email_change_failed", (exc.error_kind, logs)
                assert "Chưa đổi email" in str(exc) and "Quá 180s" in str(exc), (str(exc), logs)
            else:
                raise AssertionError(f"Expected EmailChangeError on OTP timeout; logs={logs}")
        finally:
            email_change._change_in_browser = orig_default  # type: ignore[assignment]
            email_change._DEFAULT_CHANGE_IN_BROWSER = orig_default  # type: ignore[assignment]

    assert stages == [("prepared", USER_ID)], stages
    browser_spy.assert_not_awaited()


async def check_session_otp_landing_recovers_to_password() -> None:
    import request_phase
    import session_phase

    signin_queries: list[dict[str, list[str]]] = []

    class FakeResp:
        def __init__(self, status_code: int, payload: object = None, text: str = "", url: str = ""):
            self.status_code = status_code
            self._payload = payload
            self.text = text if text else (json.dumps(payload) if payload is not None else "")
            self.url = url
            self.headers: dict[str, str] = {}

        def json(self):
            return self._payload if isinstance(self._payload, dict) else json.loads(self.text)

    class FakeJarItem:
        name = "__Secure-next-auth.session-token"
        value = "sess-ok"
        domain = ".chatgpt.com"
        path = "/"
        secure = True
        expires = None

    class FakeSession:
        def __init__(self):
            self.cookies = SimpleNamespace(
                jar=[FakeJarItem()],
                items=lambda: [("__Secure-next-auth.session-token", "sess-ok")],
            )

        def close(self):
            pass

        def get(self, url: str, **kwargs):
            if url.endswith("/api/auth/session"):
                return FakeResp(
                    200,
                    payload={"user": {"email": SOURCE, "id": USER_ID}, "accessToken": "tok-recovered"},
                    url=url,
                )
            raise AssertionError(f"Unexpected GET {url}")

        def post(self, url: str, **kwargs):
            parsed = urlsplit(url)
            if parsed.path == "/api/auth/signin/openai":
                qs = parse_qs(parsed.query)
                signin_queries.append(qs)
                return FakeResp(200, payload={"url": "https://auth.openai.com/authorize?state=pw"}, url=url)
            if parsed.path == "/api/accounts/password/verify":
                assert kwargs.get("json") == {"password": PASSWORD}
                return FakeResp(
                    200,
                    payload={"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=done"},
                    url=url,
                )
            raise AssertionError(f"Unexpected POST {url}")

    call_idx = {"n": 0}

    def nav_side_effect(sess, auth_url, log):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            return SimpleNamespace(url="https://auth.openai.com/email-verification")
        return SimpleNamespace(url="https://auth.openai.com/log-in/password")

    with patch.object(request_phase, "_create_session", lambda **kw: FakeSession()), patch.object(
        request_phase, "_step_csrf", lambda sess, log: "csrf-1"
    ), patch.object(
        request_phase, "_step_auth_url", lambda *a, **kw: "https://auth.openai.com/authorize?state=0"
    ), patch.object(
        request_phase, "_navigate_oauth_authorize", nav_side_effect
    ), patch.object(
        request_phase, "_get_sentinel_token", lambda *a, **kw: ""
    ), patch.object(
        request_phase, "_step_follow_redirects", lambda sess, url, log: ("https://chatgpt.com/api/auth/callback/openai?code=done", None)
    ), patch.object(
        request_phase, "_consume_callback", lambda *a, **kw: True
    ), patch.object(
        request_phase, "_get_session_tokens", lambda sess: {"session_token": "sess-ok"}
    ):
        recovered = await session_phase.get_session_pure_request(
            email=SOURCE,
            password=PASSWORD,
            secret="",
            mail_provider=None,
            log=lambda _: None,
        )

    assert recovered["user"]["email"] == SOURCE
    assert signin_queries and signin_queries[0].get("screen_hint") == ["login"], signin_queries
    assert signin_queries[0].get("login_hint") == [SOURCE], signin_queries


async def check_production_api_first_and_redaction() -> None:
    source_session = {
        "user": {"email": SOURCE, "id": USER_ID},
        "accessToken": "initial-token",
        "__mfa_verified": True,
        "__cookies": [
            {"name": "__Secure-next-auth.session-token", "value": "cookie-val", "domain": ".chatgpt.com"},
        ],
    }
    fresh_session = {
        "user": {"email": TARGET, "id": USER_ID},
        "__mfa_verified": True,
    }

    class FakeMailbox:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    # 1. Production (unpatched _change_in_browser): _change_via_api runs FIRST without opening browser
    async def _fake_api(**kw):
        await kw["save"]("changed_pending_verify", USER_ID)

    api_ok = AsyncMock(side_effect=_fake_api)
    login_mock = AsyncMock(side_effect=[source_session, fresh_session])
    stages1: list[tuple[str, str]] = []
    with patch.dict(
        sys.modules,
        {
            "session_phase": SimpleNamespace(get_session_pure_request=login_mock),
            "congas_mail": SimpleNamespace(CongasMailbox=FakeMailbox, CongasMailError=RuntimeError),
        },
    ), patch.object(email_change, "_change_via_api", api_ok):
        res = await change_email(
            email=SOURCE,
            password=PASSWORD,
            secret=SECRET,
            target_email=TARGET,
            mailbox_email="fixture@gmail.com",
            mailbox_key="ABCD1234EFGH",
            checkpoint=lambda s, u: _append_async(stages1, (s, u)),
        )
    assert res["email"] == TARGET and res["login_verified"] is True
    assert api_ok.await_count == 1
    assert [s for s, _ in stages1] == ["prepared", "changed_pending_verify", "verified"]

    # 2. Non-EmailChangeError exception surfaces sanitized detail and redacts secrets
    api_timeout = AsyncMock(
        side_effect=TimeoutError(f"Page load timed out pw={PASSWORD} sec={SECRET} key=ABCD1234EFGH")
    )
    browser_timeout = AsyncMock(
        side_effect=TimeoutError(f"Camoufox hydration timeout pw={PASSWORD} sec={SECRET} key=ABCD1234EFGH")
    )
    login_once = AsyncMock(return_value=source_session)
    with patch.dict(
        sys.modules,
        {
            "session_phase": SimpleNamespace(get_session_pure_request=login_once),
            "congas_mail": SimpleNamespace(CongasMailbox=FakeMailbox, CongasMailError=RuntimeError),
        },
    ), patch.object(email_change, "_change_via_api", api_timeout):
        orig_default = email_change._DEFAULT_CHANGE_IN_BROWSER
        try:
            email_change._change_in_browser = browser_timeout  # type: ignore[assignment]
            email_change._DEFAULT_CHANGE_IN_BROWSER = browser_timeout  # type: ignore[assignment]
            try:
                await change_email(
                    email=SOURCE,
                    password=PASSWORD,
                    secret=SECRET,
                    target_email=TARGET,
                    mailbox_email="fixture@gmail.com",
                    mailbox_key="ABCD1234EFGH",
                    checkpoint=lambda s, u: _append_async([], (s, u)),
                )
            except EmailChangeError as exc:
                msg = str(exc)
                assert "Camoufox hydration timeout" in msg, msg
                assert PASSWORD not in msg and SECRET not in msg and "ABCD1234EFGH" not in msg, msg
            else:
                raise AssertionError("Expected EmailChangeError")
        finally:
            email_change._change_in_browser = orig_default  # type: ignore[assignment]
            email_change._DEFAULT_CHANGE_IN_BROWSER = orig_default  # type: ignore[assignment]
    assert api_timeout.await_count == 1 and browser_timeout.await_count == 1


async def _append_async(items: list, item: object) -> None:
    items.append(item)


async def main() -> None:
    check_cookie_hygiene_and_bootstrap()
    check_congas_mime_clockskew_and_templates()
    await check_camoufox_launcher_selection()
    await check_production_dom_and_fail_fast_browser()
    await check_api_fallback_with_reauth()
    await check_api_otp_timeout_stays_prepared()
    await check_session_otp_landing_recovers_to_password()
    await check_production_api_first_and_redaction()
    print(
        "PASS Camoufox launcher, CF cookie stripping, Name==Email + EmailDialog production DOM, "
        "fail-fast eligibility/begin business rejections, Congas MIME/clock-skew/templates, "
        "API-first + Camoufox fallback, and secret-safe exception sanitization"
    )


if __name__ == "__main__":
    asyncio.run(main())
