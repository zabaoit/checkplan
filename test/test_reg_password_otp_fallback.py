"""Regression checks cho password-login → one-time-code fallback."""
from __future__ import annotations

import asyncio
import py_compile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_phase import (  # noqa: E402
    _is_authorize_navigation_aborted,
    _navigate_authorize_url,
    _switch_password_login_to_otp,
)


class _Element:
    def __init__(self, page: "_Page", text: str, *, visible: bool = True) -> None:
        self.page = page
        self.text = text
        self.visible = visible

    async def is_visible(self, timeout: int = 0) -> bool:
        return self.visible

    async def text_content(self, timeout: int = 0) -> str:
        return self.text

    async def click(self, timeout: int = 0) -> None:
        self.page.otp_visible = True
        self.page.url = "https://auth.openai.com/email-verification"


class _Collection:
    def __init__(self, items: list[_Element]) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> _Element:
        return self.items[index]


class _Locator:
    def __init__(self, page: "_Page", selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    async def is_visible(self, timeout: int = 0) -> bool:
        is_otp_selector = any(
            marker in self.selector
            for marker in ('name="code"', "one-time-code", 'inputmode="numeric"')
        )
        return self.page.otp_visible and is_otp_selector

    async def text_content(self, timeout: int = 0) -> str:
        return ""


class _Page:
    def __init__(self, actions: list[str], *, api_fallback_works: bool = True) -> None:
        self.url = "https://auth.openai.com/log-in/password"
        self.otp_visible = False
        self.api_fallback_works = api_fallback_works
        self.actions = [_Element(self, text) for text in actions]
        self.goto_urls: list[str] = []

    def locator(self, selector: str):
        if selector == 'button, a, [role="button"]':
            return _Collection(self.actions)
        return _Locator(self, selector)

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_urls.append(url)
        if self.api_fallback_works:
            self.url = "https://auth.openai.com/email-verification"
            self.otp_visible = True


class _AuthorizePage(_Page):
    def __init__(self) -> None:
        super().__init__([])
        self.url = "https://chatgpt.com/"
        self.assigned_url: str | None = None

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_urls.append(url)
        raise Exception("Page.goto: NS_BINDING_ABORTED")

    async def evaluate(self, _script: str, url: str) -> None:
        self.assigned_url = url
        self.url = "https://auth.openai.com/email-verification"
        self.otp_visible = True


def _log(message: str) -> None:
    print(message.encode("ascii", "replace").decode("ascii"), flush=True)


async def _run() -> None:
    otp_marks: list[str] = []
    page_ui = _Page(["Continue", "Email me a code"])
    source = await _switch_password_login_to_otp(
        page_ui,
        log=_log,
        mark_otp_started=lambda: otp_marks.append("ui"),
    )
    if source != "ui" or not page_ui.otp_visible or page_ui.goto_urls:
        raise AssertionError(f"UI fallback sai: source={source!r}, goto={page_ui.goto_urls!r}")

    page_api = _Page(["Continue", "Forgot password?"])
    source = await _switch_password_login_to_otp(
        page_api,
        log=_log,
        mark_otp_started=lambda: otp_marks.append("api"),
    )
    if source != "api" or not page_api.otp_visible:
        raise AssertionError(f"API fallback sai: source={source!r}")
    if page_api.goto_urls != ["https://auth.openai.com/api/accounts/email-otp/send"]:
        raise AssertionError(f"API fallback URL sai: {page_api.goto_urls!r}")
    if otp_marks != ["ui", "api"]:
        raise AssertionError(f"OTP progress marker sai: {otp_marks!r}")

    authorize_url = "https://auth.openai.com/api/accounts/authorize?state=test"
    page_authorize = _AuthorizePage()
    await _navigate_authorize_url(page_authorize, authorize_url, log=_log)
    if page_authorize.assigned_url != authorize_url or not page_authorize.otp_visible:
        raise AssertionError(
            f"authorize recovery sai: assigned={page_authorize.assigned_url!r}, "
            f"url={page_authorize.url!r}"
        )

    if not _is_authorize_navigation_aborted(Exception("NS_BINDING_ABORTED")):
        raise AssertionError("không nhận diện NS_BINDING_ABORTED")
    if _is_authorize_navigation_aborted(Exception("HTTP 403")):
        raise AssertionError("HTTP 403 không được coi là navigation-abort")


def main() -> None:
    py_compile.compile(str(ROOT / "browser_phase.py"), doraise=True)
    asyncio.run(_run())
    print("OK reg password-to-OTP fallback", flush=True)


if __name__ == "__main__":
    main()
