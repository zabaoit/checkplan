"""Verify AutoReg runner mark_dead proxy lỗi network + giữ helper signature đúng.

Kiểm:
- T01 syntax_ok
- T02 _note_proxy_failure được gọi đúng 2 chỗ (result.success=False, except Exception)
- T03 helper _note_proxy_failure tồn tại + dùng _is_proxy_network_error + ProxyPool.mark_dead
- T04 email_proxy_line được init `None` trước try (safe access trong except)
- T05 dùng mock pool: lỗi network → mark_dead; lỗi nghiệp vụ → KHÔNG mark_dead
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "autoreg" / "runner.py"
SRC = RUNNER.read_text(encoding="utf-8")


def t01_syntax_ok() -> int:
    try:
        globals()["_TREE"] = ast.parse(SRC)
    except SyntaxError as exc:  # noqa: BLE001
        print(f"[FAIL] t01 syntax :: {exc}", flush=True)
        return 1
    print("[PASS] t01 runner.py parse AST OK", flush=True)
    return 0


def t02_note_proxy_failure_call_sites() -> int:
    """Phải gọi _note_proxy_failure ít nhất 2 lần trong _process_email."""
    tree = globals()["_TREE"]
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "_note_proxy_failure":
            count += 1
    if count < 2:
        print(f"[FAIL] t02 expect ≥2 call _note_proxy_failure :: got {count}", flush=True)
        return 1
    print(f"[PASS] t02 _note_proxy_failure gọi {count} lần", flush=True)
    return 0


def t03_helper_defined() -> int:
    tree = globals()["_TREE"]
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_note_proxy_failure":
            found = True
            body_src = ast.unparse(node)
            if "_is_proxy_network_error" not in body_src:
                print("[FAIL] t03 helper không gọi _is_proxy_network_error", flush=True)
                return 1
            if "mark_dead" not in body_src:
                print("[FAIL] t03 helper không gọi mark_dead", flush=True)
                return 1
            break
    if not found:
        print("[FAIL] t03 không thấy def _note_proxy_failure", flush=True)
        return 1
    print("[PASS] t03 helper _note_proxy_failure defined + đúng API", flush=True)
    return 0


def t04_email_proxy_line_init_none() -> int:
    """Trước try, phải có `email_proxy_line: ... = None` để except branch safe."""
    if "email_proxy_line: str | None = None" not in SRC:
        print("[FAIL] t04 không thấy init email_proxy_line = None trước try", flush=True)
        return 1
    print("[PASS] t04 email_proxy_line init None trước try", flush=True)
    return 0


def t05_runtime_mark_dead_behavior() -> int:
    """Mock pool + manager helper, gọi _note_proxy_failure thực tế."""
    sys.path.insert(0, str(ROOT))
    # Setup module mock cho web.manager + web.proxy_pool trước khi import runner
    import types

    fake_manager = types.ModuleType("web.manager")
    def _is_net(exc_or_msg) -> bool:
        s = str(exc_or_msg).lower()
        return any(k in s for k in ("proxy", "timeout", "connection refused", "tunnel"))
    fake_manager._is_proxy_network_error = _is_net
    fake_manager._mask_proxy = lambda s: "***"
    sys.modules["web.manager"] = fake_manager

    fake_pool_mod = types.ModuleType("web.proxy_pool")
    class _FakePool:
        def __init__(self) -> None:
            self.dead: list[str] = []
        def mark_dead(self, line: str) -> bool:
            if line in self.dead:
                return False
            self.dead.append(line)
            return True
    _pool = _FakePool()
    fake_pool_mod.get_proxy_pool = lambda: _pool
    sys.modules["web.proxy_pool"] = fake_pool_mod

    from autoreg.runner import AutoRegRunner

    # Tạo bare instance, bỏ qua __init__ (cần nhiều dep) — chỉ test method.
    runner = AutoRegRunner.__new__(AutoRegRunner)
    async def _no_log(level, msg, meta=None):  # noqa: ARG001
        return None
    runner._log = _no_log  # type: ignore[attr-defined]

    # Cần 1 event loop để asyncio.create_task không raise
    async def _exercise() -> None:
        # Case 1: lỗi network → mark_dead
        runner._note_proxy_failure("h:1:u:p", "proxy timeout", prefix="[t]")
        # Case 2: lỗi nghiệp vụ → KHÔNG mark_dead
        runner._note_proxy_failure("h:2:u:p", "stripe declined card", prefix="[t]")
        # Case 3: line=None → no-op
        runner._note_proxy_failure(None, "proxy timeout", prefix="[t]")
        # Case 4: gọi lại line đã dead → idempotent
        runner._note_proxy_failure("h:1:u:p", "proxy timeout", prefix="[t]")
        await asyncio.sleep(0)  # cho task log chạy 1 nhịp

    asyncio.run(_exercise())

    if _pool.dead != ["h:1:u:p"]:
        print(f"[FAIL] t05 dead set sai :: {_pool.dead}", flush=True)
        return 1
    print("[PASS] t05 mark_dead chỉ kích hoạt cho lỗi network + idempotent", flush=True)
    return 0


def main() -> int:
    print("=== check_autoreg_proxy_mark_dead ===", flush=True)
    rc = 0
    for fn in (
        t01_syntax_ok,
        t02_note_proxy_failure_call_sites,
        t03_helper_defined,
        t04_email_proxy_line_init_none,
        t05_runtime_mark_dead_behavior,
    ):
        rc |= fn()
    print("=== DONE ===" if rc == 0 else "=== FAILED ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
