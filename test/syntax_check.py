"""Parse AST tất cả file Python ở root + thư mục con cần thiết.

Verify fix browser_phase.py không bị syntax error.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Các file/thư mục cần check (ưu tiên file vừa edit)
TARGETS = [
    ROOT / "browser_phase.py",
    ROOT / "request_phase.py",
    ROOT / "models.py",
]


def main() -> int:
    failed: list[tuple[Path, str]] = []
    for i, p in enumerate(TARGETS, 1):
        if not p.exists():
            print(f"[SKIP] [{i}/{len(TARGETS)}] {p} không tồn tại", flush=True)
            continue
        try:
            src = p.read_text(encoding="utf-8")
            ast.parse(src, filename=str(p))
            print(f"[PASS] [{i}/{len(TARGETS)}] {p.relative_to(ROOT)} :: AST OK ({len(src.splitlines())} lines)", flush=True)
        except SyntaxError as exc:
            failed.append((p, f"SyntaxError: {exc.msg} at line {exc.lineno}"))
            print(f"[FAIL] [{i}/{len(TARGETS)}] {p.relative_to(ROOT)} :: SyntaxError {exc.msg} (line {exc.lineno})", flush=True)
        except Exception as exc:
            failed.append((p, f"{type(exc).__name__}: {exc}"))
            print(f"[FAIL] [{i}/{len(TARGETS)}] {p.relative_to(ROOT)} :: {type(exc).__name__}: {exc}", flush=True)

    print("", flush=True)
    if failed:
        print(f"[SUMMARY] {len(failed)} file(s) FAIL", flush=True)
        for p, msg in failed:
            print(f"  - {p}: {msg}", flush=True)
        return 1
    print(f"[SUMMARY] all {len(TARGETS)} file(s) PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
