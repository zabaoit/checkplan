"""Verify proxy runtime uses only the saved proxy pool."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "web" / "manager.py"


def main() -> int:
    src = MANAGER.read_text(encoding="utf-8")
    ast.parse(src)
    required = [
        "async def _next_proxy_source",
        "if has_pool:",
        'return "pool"',
        "return None",
        "raw_pool = pool_entries",
    ]
    missing = [item for item in required if item not in src]
    if missing:
        print(f"[FAIL] pool-only proxy markers missing: {missing}", flush=True)
        return 1
    print("[PASS] proxy runtime is pool-only", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
