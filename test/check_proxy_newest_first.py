from __future__ import annotations

from pathlib import Path
import py_compile


ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT))

from web.proxy_pool import ProxyPool
from web.proxy_format import mask_proxy, set_show_raw_proxy_logs


def check_pool_cursor_resets_to_newest_first() -> None:
    pool = ProxyPool()
    pool.configure(["old-1", "old-2"], mode="round_robin")
    assert pool.pick() == "old-1"
    pool.configure(["new-1", "old-1", "old-2"], mode="round_robin")
    assert pool.pick() == "new-1"
    assert pool.pick() == "old-1"


def check_round_robin_acquire_is_strict_order() -> None:
    pool = ProxyPool()
    pool.configure(["p1", "p2", "p3"], mode="round_robin")
    assert pool.acquire() == "p1"
    assert pool.acquire() == "p2"
    assert pool.acquire() == "p3"

    pool.configure(["p1", "p2", "p3"], mode="round_robin")
    assert pool.ordered_for_job(first="p2") == ["p2", "p3", "p1"]


def check_settings_ui_prepends_new_rows() -> None:
    js = (ROOT / "web" / "static" / "settings_panel.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "state.rows.unshift(makeRow(\"\"));" in js
    assert "state.rows = newRows.concat(state.rows);" in js
    assert "inputs[0].focus()" in js
    assert "function moveRow(rowId, delta)" in js
    assert "proxy-pool-move-up" in js
    assert "proxy-pool-move-down" in js
    assert 'id="proxy-pool-shuffle"' in html
    assert "function shuffleRowsAndSave()" in js
    assert "Math.random()" in js
    assert "save();" in js


def check_proxy_test_is_ordered() -> None:
    server = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "results = []" in server
    assert "for proxy in targets:" in server
    assert "results.append(await _probe_one_proxy(proxy))" in server
    assert "asyncio.gather(*[_probe_one_proxy(p) for p in targets])" not in server


def check_python_syntax() -> None:
    py_compile.compile(str(ROOT / "db" / "repositories.py"), doraise=True)
    py_compile.compile(str(ROOT / "web" / "proxy_format.py"), doraise=True)
    py_compile.compile(str(ROOT / "web" / "manager.py"), doraise=True)
    py_compile.compile(str(ROOT / "web" / "proxy_pool.py"), doraise=True)
    py_compile.compile(str(ROOT / "web" / "server.py"), doraise=True)


def check_proxy_mask_toggle() -> None:
    proxy = "http://user:pass@example.com:8080"
    set_show_raw_proxy_logs(False)
    assert mask_proxy(proxy) == "http://***@example.com:8080"
    set_show_raw_proxy_logs(True)
    assert mask_proxy(proxy) == proxy
    set_show_raw_proxy_logs(False)


def main() -> int:
    check_pool_cursor_resets_to_newest_first()
    check_round_robin_acquire_is_strict_order()
    check_settings_ui_prepends_new_rows()
    check_proxy_test_is_ordered()
    check_proxy_mask_toggle()
    check_python_syntax()
    print("OK proxy newest-first behavior")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
