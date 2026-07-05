"""Verify AutoReg runner gọi _resolve_job_proxy đúng: async (await) + unpack tuple.

Bug cũ: `email_proxy = _resolve_job_proxy()` (thiếu await, không unpack) khiến
proxy = coroutine → mọi email chạy direct cùng IP (không random per-email).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "autoreg" / "runner.py"


def t01_parse_ok() -> int:
    try:
        tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # noqa: BLE001
        print(f"[FAIL] t01 syntax :: {exc}", flush=True)
        return 1
    print("[PASS] t01 runner.py parse AST OK", flush=True)
    globals()["_TREE"] = tree
    return 0


def t02_call_is_awaited_and_unpacked() -> int:
    tree = globals()["_TREE"]
    found = 0
    for node in ast.walk(tree):
        # Tìm call _resolve_job_proxy(...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_resolve_job_proxy":
            found += 1
            # Phải nằm trong Await
            parent_await = any(
                isinstance(a, ast.Await) and a.value is node
                for a in ast.walk(tree)
            )
            if not parent_await:
                print("[FAIL] t02 call _resolve_job_proxy không nằm trong await", flush=True)
                return 1
    if found == 0:
        print("[FAIL] t02 không tìm thấy call _resolve_job_proxy", flush=True)
        return 1
    print(f"[PASS] t02 {found} call _resolve_job_proxy đều được await", flush=True)
    return 0


def t03_assign_unpacks_tuple() -> int:
    """Kết quả phải unpack 2 phần tử (url, line), không gán single name."""
    tree = globals()["_TREE"]
    ok = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if isinstance(val, ast.Await) and isinstance(val.value, ast.Call) \
                and isinstance(val.value.func, ast.Name) \
                and val.value.func.id == "_resolve_job_proxy":
            tgt = node.targets[0]
            if isinstance(tgt, ast.Tuple) and len(tgt.elts) == 2:
                ok = True
            else:
                print("[FAIL] t03 kết quả không unpack thành (url, line)", flush=True)
                return 1
    if not ok:
        print("[FAIL] t03 không thấy assign unpack await _resolve_job_proxy", flush=True)
        return 1
    print("[PASS] t03 unpack (url, line) đúng", flush=True)
    return 0


def main() -> int:
    print("=== check_autoreg_proxy_await ===", flush=True)
    rc = 0
    for fn in (t01_parse_ok, t02_call_is_awaited_and_unpacked, t03_assign_unpacks_tuple):
        rc |= fn()
    print("=== DONE ===" if rc == 0 else "=== FAILED ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
