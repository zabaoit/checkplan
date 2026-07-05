"""Smoke check: Reg max_concurrent cap = 2 ở mọi nguồn (Settings, Manager, Server)."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parse(p: Path) -> ast.AST:
    src = p.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(p))


def main() -> int:
    failures: list[str] = []

    # 1. Syntax check toàn bộ file đã sửa
    files = [
        ROOT / "web" / "server.py",
        ROOT / "web" / "manager.py",
        ROOT / "db" / "repositories.py",
    ]
    for f in files:
        try:
            parse(f)
            print(f"[PASS] syntax {f.relative_to(ROOT)}", flush=True)
        except SyntaxError as e:
            failures.append(f"syntax {f}: {e}")
            print(f"[FAIL] syntax {f.relative_to(ROOT)} :: {e}", flush=True)

    # 2. SettingsRepository validate reg.max_concurrent ∈ [1, 2]
    from db.repositories import _validate_type_constraint, RepositoryError

    # 1, 2 phải pass
    for v in (1, 2):
        try:
            _validate_type_constraint("reg.max_concurrent", v)
            print(f"[PASS] settings accept reg.max_concurrent={v}", flush=True)
        except RepositoryError as e:
            failures.append(f"settings reject {v}: {e}")
            print(f"[FAIL] settings reject {v} :: {e}", flush=True)

    # 3, 5, 10 phải bị reject
    for v in (3, 5, 10):
        try:
            _validate_type_constraint("reg.max_concurrent", v)
            failures.append(f"settings ACCEPT {v} (expected reject)")
            print(f"[FAIL] settings ACCEPT {v} (expected reject)", flush=True)
        except RepositoryError as e:
            print(f"[PASS] settings reject reg.max_concurrent={v} :: {e.cause}", flush=True)

    # 3. JobManager.set_max_concurrent
    import asyncio
    from web.manager import JobManager

    async def _check_jm() -> list[str]:
        local_fail: list[str] = []
        jm = JobManager(max_concurrent=1)
        try:
            jm.set_max_concurrent(2)
            print(f"[PASS] JobManager.set_max_concurrent(2) ok, _max={jm.max_concurrent}", flush=True)
        except ValueError as e:
            local_fail.append(f"JobManager 2 fail: {e}")
            print(f"[FAIL] JobManager.set_max_concurrent(2) :: {e}", flush=True)

        for v in (3, 5, 10):
            try:
                jm.set_max_concurrent(v)
                local_fail.append(f"JobManager ACCEPT {v}")
                print(f"[FAIL] JobManager.set_max_concurrent({v}) ACCEPT (expected raise)", flush=True)
            except ValueError as e:
                print(f"[PASS] JobManager reject {v} :: {e}", flush=True)

        # apply_settings cap
        jm2 = JobManager(max_concurrent=1)
        jm2.apply_settings({"reg.max_concurrent": 7})
        if jm2.max_concurrent == 2:
            print(f"[PASS] apply_settings cap 7 → 2", flush=True)
        else:
            local_fail.append(f"apply_settings 7 → {jm2.max_concurrent} (expected 2)")
            print(f"[FAIL] apply_settings 7 → {jm2.max_concurrent}", flush=True)
        return local_fail

    failures.extend(asyncio.run(_check_jm()))

    # 5. Server set_config clamp logic — kiểm tra qua đọc nguồn
    server_src = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    if "max(1, min(payload.max_concurrent, 2))" in server_src:
        print(f"[PASS] server.py clamp max_concurrent → 2", flush=True)
    else:
        failures.append("server.py không clamp về 2")
        print(f"[FAIL] server.py không thấy clamp về 2", flush=True)

    # 6. Frontend cap
    js_src = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    if "Math.min(raw, 2)" in js_src:
        print(f"[PASS] app.js cap _modeToConcurrency → 2", flush=True)
    else:
        failures.append("app.js không cap _modeToConcurrency về 2")
        print(f"[FAIL] app.js không cap _modeToConcurrency về 2", flush=True)

    print("", flush=True)
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===", flush=True)
        for x in failures:
            print(f"  - {x}", flush=True)
        return 1
    print("=== ALL PASS ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
