"""Parse AST for Telegram Plus button changes."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "web" / "telegram_notifier.py",
    ROOT / "web" / "manager.py",
    ROOT / "web" / "server.py",
]


def main() -> None:
    for path in TARGETS:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        print(f"OK {path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
