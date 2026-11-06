"""Read-only Account probe: uses source login, never clicks or changes an email."""
import argparse
import asyncio
import json
import re
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import load_settings
from email_change import EmailChangeError, _email_edit_control, session_cookies, session_identity, _open_account_page
from session_phase import get_session_pure_request
from web.auth import get_token
from playwright.async_api import async_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--email", required=True)
parser.add_argument("--clean-browser-cookies", action="store_true")
parser.add_argument("--browser", choices=("msedge", "chrome"), default="msedge")
args = parser.parse_args()
headers = {"X-API-Token": get_token()}


def fetch(path):
    request = urllib.request.Request("http://127.0.0.1:8085" + path, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def redact(text):
    return re.sub(r"[\w.+-]+@[\w.-]+", "[email]", str(text))


async def main():
    jobs = fetch("/api/change-password/jobs")["jobs"]
    job = next(job for job in reversed(jobs) if job["email"].casefold() == args.email.casefold())
    assert job["status"] == "error" and job["email_change_stage"] == "prepared"
    detail = fetch("/api/change-password/jobs/" + job["id"])
    assert detail["current_password"] and detail["secret"]
    settings = load_settings()
    logs = []
    print("SOURCE_LOGIN_START", flush=True)
    session = await get_session_pure_request(
        email=detail["email"], password=detail["current_password"], secret=detail["secret"],
        proxy=None, log=logs.append)
    identity, _ = session_identity(session)
    assert identity == detail["email"].casefold() and session.get("__mfa_verified") is True
    print("SOURCE_LOGIN_OK", flush=True)
    errors, responses, failures = [], [], []
    result = {"browser": args.browser, "headless": settings.browser_headless, "snapshots": []}
    out = ROOT / "test" / "out"
    out.mkdir(exist_ok=True)
    name = "account_probe_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    async with async_playwright() as pw:
        with tempfile.TemporaryDirectory(prefix="account_probe_", dir=ROOT / "test") as profile:
            context = await pw.chromium.launch_persistent_context(
                profile, channel=args.browser, headless=settings.browser_headless, locale="en-US")
            try:
                cookies = session_cookies(session)
                result["source_cookie_names"] = sorted({cookie["name"] for cookie in cookies})
                if args.clean_browser_cookies:
                    cookies = [cookie for cookie in cookies if cookie["name"] not in {"cf_clearance", "__cf_bm", "_cfuvid"}]
                result["imported_cookie_names"] = sorted({cookie["name"] for cookie in cookies})
                await context.add_cookies(cookies)
                page = context.pages[0]
                page.on("pageerror", lambda error: errors.append(redact(error)[:400]))

                def response_event(response):
                    parsed = urlsplit(response.url)
                    if response.request.resource_type in {"fetch", "xhr", "document"} or response.status >= 400:
                        responses.append([parsed.hostname, redact(parsed.path), response.status, response.headers.get("cf-mitigated"), response.headers.get("content-type")])

                def failed_event(request):
                    parsed = urlsplit(request.url)
                    failures.append([parsed.hostname, redact(parsed.path), request.failure])

                page.on("response", response_event)
                page.on("requestfailed", failed_event)
                for phase in (("clean_browser",) if args.clean_browser_cookies else ("direct", "reopen_after_denial")):
                    await _open_account_page(page, lambda line: print(line, flush=True))

                    live = await page.evaluate("""async () => {
                        const r = await fetch('/api/auth/session', {credentials: 'include'});
                        return r.ok ? await r.json() : null;
                    }""")
                    assert session_identity(live or {})[0] == identity
                    try:
                        control = await _email_edit_control(page, detail["email"])
                        selected = {"tag": await control.evaluate("e=>e.tagName"),
                                    "label": redact(await control.get_attribute("aria-label") or await control.get_attribute("title") or "")}
                        error = None
                    except EmailChangeError as exc:
                        selected, error = None, str(exc)
                    snapshot = await page.evaluate("""email => {
                        const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
                        const source = [...document.querySelectorAll('body *')].filter(e =>
                            visible(e) && e.textContent.trim().toLowerCase() === email.toLowerCase() &&
                            ![...e.children].some(c => c.textContent.trim().toLowerCase() === email.toLowerCase()));
                        return {
                            path: location.pathname,
                            title: document.title,
                            buttons: [...document.querySelectorAll('button,[role="button"]')].filter(visible).map(e => ({
                                tag:e.tagName, text:e.textContent.trim().slice(0,100), label:e.getAttribute('aria-label'),title:e.title,
                                disabled:e.disabled, testid:e.getAttribute('data-testid')
                            })),
                            rows: source.map(e => {
                                const ancestors = [];
                                for(let n=e, i=0;n && i<5;n=n.parentElement,i++){
                                    ancestors.push({tag:n.tagName, class:n.className, text:n.textContent.trim().slice(0,1000),
                                        html:n.outerHTML.slice(0,10000)});
                                }
                                return ancestors;
                            })
                        };
                    }""", detail["email"])
                    snapshot.update(phase=phase, selected=selected, error=error)
                    result["snapshots"].append(snapshot)
                    await page.screenshot(path=str(out / (name + "_" + phase + ".png")), full_page=False)
                    print("ACCOUNT_PROBE", phase, json.dumps({"selected": selected, "error": error,
                        "buttons": len(snapshot["buttons"]), "email_nodes": len(snapshot["rows"])}, ensure_ascii=True), flush=True)
            finally:
                result.update(responses=responses, failures=failures, page_errors=errors)
                destination = out / (name + ".json")
                destination.write_text(redact(json.dumps(result, ensure_ascii=False, indent=2)), encoding="utf-8")
                print("DIAGNOSTIC", destination, flush=True)
                await context.close()


try:
    asyncio.run(main())
except EmailChangeError as exc:
    print("ACCOUNT_PROBE_BLOCKED", str(exc), flush=True)
    raise SystemExit(2)
