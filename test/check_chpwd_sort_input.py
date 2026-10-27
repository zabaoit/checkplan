from collections import Counter
from pathlib import Path
import re
import sys
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main():
    if "--live" in sys.argv:
        for path, marker in (("/", b'chpwd-btn-sort-input'), ("/static/change_password.js", b"rank(a) - rank(b)")):
            with urlopen("http://127.0.0.1:8085" + path, timeout=15) as response:
                assert marker in response.read(), f"Docker asset is outdated: {path}"
        print("PASS: Docker serves updated input sort button and handler", flush=True)
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
    script = (ROOT / "web/static/change_password.js").read_text(encoding="utf-8")
    gmail = ' z+tag@GMAIL.COM |p|SECRET|{"email":"keep@other.com"} '
    googlemail = "b@googlemail.com|password|SECRET"
    lines = [
        "x@outlook.com|mentions@gmail.com", googlemail, gmail,
        "# keep comment", "not-an-email|p", "a@gmail.com|p",
        googlemail, "y@GOOGLEMAIL.COM|p", "spoof@gmail.com.evil|p", "",
    ]
    expected = [lines[i] for i in (2, 5, 1, 6, 7, 0, 3, 4, 8, 9)]
    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        page.goto("http://sort-input.test/", wait_until="domcontentloaded")
        page.evaluate("""() => {
            window.savedInput = null;
            window.GptUi = {
                getAuthToken: () => '',
                persistTextarea: (key, value) => { window.savedInput = value; }
            };
            window.SseBus = { on() {} };
            window.fetch = async () => ({ ok: true, json: async () => ({}) });
        }""")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_script_tag(content=script)
        page.locator("#tab-change-password").evaluate("el => el.style.display = 'grid'")
        page.locator("#chpwd-mode").select_option("check_plan")
        field = page.locator("#chpwd-combo-input")
        button = page.locator("#chpwd-btn-sort-input")
        field.fill("\n".join(lines))
        button.click()
        assert field.input_value().split("\n") == expected
        assert Counter(field.input_value().split("\n")) == Counter(lines)
        assert page.locator("#chpwd-combo-count").inner_text() == "8 combos"
        assert page.evaluate("window.savedInput") == "\n".join(expected)
        button.click()
        assert field.input_value() == "\n".join(expected)
        for text in ("", "single@gmail.com|p", "# only comment\ninvalid|p\n"):
            field.fill(text)
            button.click()
            assert field.input_value() == text
        assert not errors, errors
        browser.close()
    print("PASS: grouping, stable order, duplicates, comments, invalid domains, persistence, empty input")


if __name__ == "__main__":
    main()
