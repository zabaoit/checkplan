"""Real local Edge checks of production email UI. Every request stays offline."""
import asyncio
import json
import sys
import tempfile
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from email_change import EmailChangeError, _change_in_browser, _email_edit_control, _open_account_page
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            trigger = "document.getElementById('edit').showModal()"
            icon = '<svg width="24" height="24"><path d="M2 2L20 20" stroke="black"/></svg>'
            email = '<span>fixture@example.com</span>'
            named = f'<button aria-label="Change email: fixture@example.com" onclick="{trigger}">{icon}</button>'
            variants = [
                f'<div><span>Email</span>{email}<div role="button" tabindex="0" onclick="{trigger}">{icon}</div></div>',
                f'<div><span>Email</span>{email}<div onclick="{trigger}">{icon}</div></div>',
                f'<button onclick="{trigger}"><span>Email</span>{email}{icon}</button>',
                f'<div role="button" tabindex="0" onclick="{trigger}"><span>Email</span>{email}{icon}</div>',
                f'<div><span>Email</span>{email}<button>Copy</button><button aria-label="Edit email" onclick="{trigger}">{icon}</button></div>',
                f'<div><span>Email</span>{email}<span id="late"></span></div><script>setTimeout(()=>{{late.innerHTML="<button onclick=\\"{trigger}\\">Edit email</button>"}},100)</script>',
                # Closest labeled ancestor contains only text; pencil is its sibling.
                f'<div><div><span>Email</span>{email}</div>{named}</div>',
                f'<div><div><span>Email</span>{email}</div><span id="late"></span></div><script>setTimeout(()=>{{late.innerHTML={json.dumps(named)}}},100)</script>',
                f'<div><div><span>Email</span>{email}</div>{named.replace("aria-label", "title")}</div>',
            ]
            for index, markup in enumerate(variants):
                await page.set_content('<div><span>Name</span><button>Other edit</button></div>'
                    + '<div><span>Username</span><button>Other edit</button></div>' + markup
                    + '<dialog id="edit"><input type="email"></dialog>')
                if index < 2:
                    old = page.get_by_text('fixture@example.com', exact=True).locator('xpath=ancestor::*[count(.//button)=1][1]').get_by_role('button')
                    assert await old.count() == 0, 'fixture must reproduce previous failure'
                control = await _email_edit_control(page, 'fixture@example.com')
                await control.click()
                await page.locator('input[type="email"]:visible').wait_for(state='visible', timeout=1000)
            for markup in (
                f'<div><span>Email</span>{email}{icon}{icon}</div>',
                f'<div><span>Email</span>{email}<button>Edit</button></div>{email}',
                f'<div><span>Email</span>{email}{named}{named}</div>',
            ):
                await page.set_content(markup)
                try:
                    await _email_edit_control(page, 'fixture@example.com')
                except EmailChangeError:
                    pass
                else:
                    raise AssertionError('ambiguous edit target accepted')

            # Exercise the actual browser flow with the labels visible in user screenshots.
            markup = f'''
                <div><span>Name</span><button>Name pencil</button></div>
                <div><span>Username</span><button>Username pencil</button></div>
                <div><div><span>Email</span>{email}</div>{named}</div>
                <dialog id="edit">
                  <h2>Edit email</h2>
                  <label for="new-email">Email address</label><input id="new-email" type="email">
                  <button>Cancel</button><button id="send">Send verification email</button>
                </dialog>
                <dialog id="otp"><input autocomplete="one-time-code" inputmode="numeric">
                  <button id="confirm">Verify</button></dialog>
                <script>
                document.getElementById('send').onclick=()=>{{document.querySelector('#edit').close(); document.querySelector('#otp').showModal();}};
                document.getElementById('confirm').onclick=()=>{{if(document.querySelector('#otp input').value==='012345'){{
                  document.querySelector('#otp').close(); document.body.insertAdjacentHTML('beforeend','<p>Email changed</p>');
                }}}};
                </script>
            '''
            state, events, requests = {}, [], []
            identity = {"user": {"email": "fixture@example.com", "id": "stable-id"}}

            async def route_request(route):
                path = urlsplit(route.request.url).path
                requests.append(path)
                if path == "/settings/account":
                    body = markup
                    if state.get("blocked"):
                        body = markup.replace(named, "") + '<script>fetch("/backend-api/accounts/change_email/eligibility")</script>'
                    await route.fulfill(content_type="text/html", body=body)
                elif path == "/backend-api/accounts/change_email/eligibility":
                    await route.fulfill(status=403, headers={"cf-mitigated": "challenge"}, body="blocked")
                elif path == "/api/auth/session":
                    await route.fulfill(content_type="application/json", body=json.dumps(identity))
                else:
                    await route.abort()

            async def launch(**options):
                assert options["headless"] is True
                context = await browser.new_context()
                await context.route("**/*", route_request)
                state["page"] = await context.new_page()
                return context

            @asynccontextmanager
            async def offline_playwright():
                yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

            async def save(stage, user_id):
                assert user_id == "stable-id"
                events.append(stage)

            async def prepare():
                assert await state["page"].locator("#new-email").input_value() == "fixture+abc@gmail.com"
                events.append("prepare")

            async def poll_otp(**kwargs):
                assert kwargs["recipient"] == "fixture+abc@gmail.com"
                assert kwargs["started_at"].tzinfo is not None
                assert events == ["prepared", "prepare", "submitted"]
                assert await state["page"].locator("#otp").is_visible()
                return "012345"

            with tempfile.TemporaryDirectory(prefix="email_browser_", dir=ROOT / "test") as profiles:
                settings = SimpleNamespace(browser_headless=True, profiles_dir=Path(profiles))
                with patch("playwright.async_api.async_playwright", offline_playwright), patch.dict(sys.modules, {
                    "config": SimpleNamespace(load_settings=lambda: settings),
                    "browser_momo": SimpleNamespace(_parse_playwright_proxy=lambda _: None),
                }):
                    await _change_in_browser(
                        email="fixture@example.com", target_email="fixture+abc@gmail.com",
                        session={**identity, "sessionToken": "offline-cookie"}, proxy=None,
                        mailbox=SimpleNamespace(prepare=prepare, poll_otp=poll_otp), save=save)
            assert events == ["prepared", "prepare", "submitted", "changed_pending_verify"], events
            assert requests == ["/settings/account", "/api/auth/session"], requests
            events.clear()
            requests.clear()
            state["blocked"] = True
            logs = []
            with tempfile.TemporaryDirectory(prefix="email_blocked_", dir=ROOT / "test") as profiles:
                settings = SimpleNamespace(browser_headless=True, profiles_dir=Path(profiles))
                with patch("playwright.async_api.async_playwright", offline_playwright), patch.dict(sys.modules, {
                    "config": SimpleNamespace(load_settings=lambda: settings),
                    "browser_momo": SimpleNamespace(_parse_playwright_proxy=lambda _: None),
                }):
                    try:
                        await _change_in_browser(
                            email="fixture@example.com", target_email="fixture+abc@gmail.com",
                            session={**identity, "sessionToken": "offline-cookie"}, proxy=None,
                            mailbox=SimpleNamespace(prepare=prepare, poll_otp=poll_otp), save=save, log=logs.append)
                    except EmailChangeError as exc:
                        assert "HTTP 403" in str(exc) and "Cloudflare" in str(exc), str(exc)
                    else:
                        raise AssertionError("Cloudflare eligibility denial accepted")
            assert events == ["prepared"], events
            assert requests.count("/backend-api/accounts/change_email/eligibility") == 1, requests

            challenge_page = await browser.new_page()
            loads = []

            async def challenge_route(route):
                loads.append(route.request.url)
                await route.fulfill(status=403, headers={"cf-mitigated": "challenge"},
                    content_type="text/html", body="<p>Verify access</p>")

            await challenge_page.route("**/*", challenge_route)
            logs = []
            try:
                await _open_account_page(challenge_page, logs.append)
            except EmailChangeError as exc:
                assert "HTTP 403" in str(exc) and "Cloudflare" in str(exc), str(exc)
            else:
                raise AssertionError("Cloudflare challenge accepted as Account")
            assert len(loads) == 1 and any("Cloudflare" in line for line in logs)
            await challenge_page.close()
            print("PASS production selector: native/role/icon/nested/delayed/title rows, ambiguity; actual browser flow: Edit email, Send verification email, OTP; Cloudflare page/eligibility denial blocks mutation; Playwright", version("playwright"))
        finally:
            await browser.close()


asyncio.run(main())
