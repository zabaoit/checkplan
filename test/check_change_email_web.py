"""Read-only Edge smoke for live Change mail controls; never starts account jobs."""
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    screenshots = ROOT / "test" / "ui_screenshots"
    screenshots.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        blocked_writes = []

        def read_only(route):
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                request_path = urlsplit(route.request.url).path
                blocked_writes.append((route.request.method, request_path))
                route.fulfill(status=200, content_type="application/json", body="{}")
            else:
                route.continue_()

        context.route("**/*", read_only)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        with page.expect_request(lambda request: request.method == "PUT" and urlsplit(request.url).path == "/api/settings/ui.active_tab"):
            page.goto("http://127.0.0.1:8085/", wait_until="domcontentloaded")
        page.locator('[data-tab="change-password"]').click()
        mode = page.locator("#chpwd-mode")
        expect(mode.locator('option[value="change_email"]')).to_have_text("Change mail")
        expect(page.locator("#chpwd-congas-panel")).to_be_hidden()
        mode.select_option("change_email")
        expect(page.locator("#chpwd-congas-panel")).to_be_visible()
        expect(page.locator("#chpwd-congas-input")).to_be_visible()
        expect(page.locator("#chpwd-new-password")).to_be_disabled()
        assert page.locator("#chpwd-congas-input").get_attribute("placeholder") == "abc@gmail.com|token"
        assert page.evaluate("localStorage.getItem('gpt_reg.input.change_password')") is None
        # Fresh browser context, no credentials entered; mask live results before local screenshot.
        screenshot = screenshots / "change_mail_tab.png"
        page.locator("#tab-change-password").screenshot(
            path=str(screenshot),
            mask_color="#15191d",
            mask=[page.locator("#chpwd-job-list"), page.locator("#chpwd-log-pane"), page.locator("#tab-change-password .output-pane")],
        )
        mode.select_option("change_2fa")
        expect(page.locator("#chpwd-congas-panel")).to_be_hidden()
        expect(page.locator("#chpwd-new-password")).to_be_enabled()
        assert not errors, errors
        allowed_settings = {"/api/settings", "/api/change-password/config"}
        unexpected = [(method, path) for method, path in blocked_writes if path not in allowed_settings and not path.startswith("/api/settings/")]
        assert not unexpected, f"Unexpected write attempted during read-only UI smoke: {unexpected}"
        print("Mocked settings writes:", blocked_writes)
        context.close()
        browser.close()
    print("PASS live Edge Change mail controls, old mode toggle, no writes; screenshot test/ui_screenshots/change_mail_tab.png")


if __name__ == "__main__":
    main()
