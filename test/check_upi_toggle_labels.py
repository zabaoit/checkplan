"""Verify UPI proxy/Telegram toggle labels remain readable in the UI."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    index_src = _read("web/static/index.html")
    css_src = _read("web/static/style.css")

    required_html = [
        "upi-control-toggle upi-proxy-control",
        "Dùng Proxy",
        "upi-control-toggle upi-telegram-control",
        "Gửi Telegram",
    ]
    required_css = [
        "#tab-upi .card-settings-row-mixed",
        "flex-wrap: wrap",
        "#tab-upi .upi-control-toggle",
        "min-width: 108px",
        "#tab-upi .upi-telegram-control",
        "min-width: 152px",
        "white-space: nowrap",
    ]

    missing_html = [item for item in required_html if item not in index_src]
    missing_css = [item for item in required_css if item not in css_src]
    if missing_html or missing_css:
        print(f"[FAIL] html={missing_html} css={missing_css}", flush=True)
        return 1

    print("[PASS] UPI proxy/Telegram toggle labels are explicit and non-wrapping", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
