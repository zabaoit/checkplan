"""Verify Get Session PLUS badge UI is clear and styled."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures: list[str] = []
    session_js = (ROOT / "web" / "static" / "session.js").read_text(encoding="utf-8")
    style_css = (ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")

    checks = [
        ("planKey lowercases plan_type", "const planKey = j.plan_type ? String(j.plan_type).toLowerCase() : '';" in session_js),
        ("PLUS label is uppercase", "planKey === 'plus' ? 'PLUS'" in session_js),
        ("plan badge has title", 'title="Plan: ${escHtml(planLabel)}"' in session_js),
        ("plan badge rounded pill", "border-radius: 999px;" in style_css),
        ("plus badge has bright foreground", "color: #d9ffe2;" in style_css),
        ("plus badge has green border", "border-color: rgba(123, 216, 143, 0.8);" in style_css),
        ("plus badge has glow", "0 0 14px rgba(123, 216, 143, 0.18)" in style_css),
    ]

    for label, ok in checks:
        if ok:
            print(f"[PASS] {label}", flush=True)
        else:
            failures.append(label)
            print(f"[FAIL] {label}", flush=True)

    if failures:
        print(f"\n=== {len(failures)} FAILURE(S) ===", flush=True)
        for item in failures:
            print(f"- {item}", flush=True)
        return 1

    print("\n=== ALL PASS ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
