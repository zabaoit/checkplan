from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "change_password.py",
    ROOT / "web" / "change_password_manager.py",
    ROOT / "web" / "server.py",
    ROOT / "db" / "repositories.py",
]


def main() -> None:
    for path in FILES:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        print(f"OK {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
