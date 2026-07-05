from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "runtime" / "data.db"
TOKEN_DIR = ROOT / "runtime" / "upi_tokens"

conn = sqlite3.connect(DB)
cur = conn.execute("SELECT value FROM settings WHERE key='proxy.pool'")
row = cur.fetchone()
current = set(json.loads(row[0]) if row else [])

seen = []
seen_set = set()
for path in sorted(TOKEN_DIR.glob("*.json")):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    proxy = data.get("proxy")
    if proxy and proxy not in seen_set:
        seen.append(proxy)
        seen_set.add(proxy)

missing = [p for p in seen if p not in current]
print(f"current_count={len(current)}")
print(f"historical_token_proxy_count={len(seen)}")
print(f"historical_not_in_current={len(missing)}")
for p in missing:
    print(p)
