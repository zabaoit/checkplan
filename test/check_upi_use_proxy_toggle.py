"""Smoke check: UPI proxy toggle wiring across Settings, manager, API, UI."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def main() -> int:
    failures: list[str] = []

    for rel in (
        "db/repositories.py",
        "web/manager.py",
        "web/server.py",
    ):
        try:
            parse(ROOT / rel)
            print(f"[PASS] syntax {rel}", flush=True)
        except SyntaxError as exc:
            failures.append(f"syntax {rel}: {exc}")
            print(f"[FAIL] syntax {rel}: {exc}", flush=True)

    from db.repositories import RepositoryError, _EXACT_KEYS, _validate_type_constraint

    if "upi.use_proxy" in _EXACT_KEYS:
        print("[PASS] upi.use_proxy in Settings whitelist", flush=True)
    else:
        failures.append("upi.use_proxy missing from _EXACT_KEYS")

    for value in (True, False):
        try:
            _validate_type_constraint("upi.use_proxy", value)
            print(f"[PASS] validator accepts upi.use_proxy={value}", flush=True)
        except RepositoryError as exc:
            failures.append(f"validator rejects {value}: {exc}")

    for value in ("true", 1, 0, None):
        try:
            _validate_type_constraint("upi.use_proxy", value)
            failures.append(f"validator accepts non-bool {value!r}")
            print(f"[FAIL] validator accepts non-bool {value!r}", flush=True)
        except RepositoryError:
            print(f"[PASS] validator rejects non-bool {value!r}", flush=True)

    from web.manager import UpiJobManager

    mgr = UpiJobManager()
    if mgr.use_proxy is True:
        print("[PASS] UpiJobManager default use_proxy=True", flush=True)
    else:
        failures.append(f"default use_proxy={mgr.use_proxy}")
    mgr.set_use_proxy(False)
    if mgr.use_proxy is False:
        print("[PASS] UpiJobManager.set_use_proxy(False)", flush=True)
    else:
        failures.append("set_use_proxy(False) failed")
    mgr.apply_settings({"upi.use_proxy": True})
    if mgr.use_proxy is True:
        print("[PASS] UpiJobManager.apply_settings hydrates upi.use_proxy", flush=True)
    else:
        failures.append("apply_settings did not hydrate upi.use_proxy")
    mgr.shutdown()

    from web.server import SetUpiConfigRequest

    req = SetUpiConfigRequest(use_proxy=False)
    if req.use_proxy is False:
        print("[PASS] SetUpiConfigRequest accepts use_proxy", flush=True)
    else:
        failures.append("SetUpiConfigRequest.use_proxy failed")

    manager_src = (ROOT / "web" / "manager.py").read_text(encoding="utf-8")
    server_src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    html_src = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    js_src = (ROOT / "web" / "static" / "upi.js").read_text(encoding="utf-8")

    for needle, label in (
        ("if self._use_proxy else None", "manager skips acquire when disabled"),
        ("if self._use_proxy else []", "manager passes empty pool when disabled"),
        ('settings_writes["upi.use_proxy"] = payload.use_proxy', "server write-through"),
        ('"use_proxy": um.use_proxy', "server responses include use_proxy"),
        ('id="upi-proxy-toggle"', "HTML toggle present"),
        ("proxyToggle:   $('upi-proxy-toggle')", "JS dom binding"),
        ("JSON.stringify({ use_proxy: enabled })", "JS change save"),
        ("use_proxy: !!dom.proxyToggle.checked", "JS run config save"),
    ):
        haystack = manager_src + server_src + html_src + js_src
        if needle in haystack:
            print(f"[PASS] {label}", flush=True)
        else:
            failures.append(f"missing: {label} :: {needle}")
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
