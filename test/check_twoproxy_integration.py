"""Verify 2Proxy rotation manager, settings integration, and lease/release behavior.

Pure-sync & async mock test, no network required.
Convention: tNN -> int, [PASS]/[FAIL], main() exit 0 = all pass.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import types
sys.modules["web"] = types.ModuleType("web")

import db.engine
import db.repositories as repos
from db.repositories import SettingsRepository, RepositoryError

_pf_spec = importlib.util.spec_from_file_location("web.proxy_format", ROOT / "web" / "proxy_format.py")
_pf = importlib.util.module_from_spec(_pf_spec)
_pf_spec.loader.exec_module(_pf)
sys.modules["web.proxy_format"] = _pf
sys.modules["proxy_format"] = _pf

_tp_spec = importlib.util.spec_from_file_location("web.twoproxy_pool", ROOT / "web" / "twoproxy_pool.py")
_tp = importlib.util.module_from_spec(_tp_spec)
_tp_spec.loader.exec_module(_tp)
sys.modules["web.twoproxy_pool"] = _tp

TwoProxyPool = _tp.TwoProxyPool
normalize_keys = _tp.normalize_keys
mask_key = _tp.mask_key
get_twoproxy_pool = _tp.get_twoproxy_pool


def t01_normalize_and_mask() -> int:
    keys = normalize_keys("key1\nkey2\n  key1  \n\nkey3\n")
    if keys != ["key1", "key2", "key3"]:
        print(f"[FAIL] t01 normalize_keys :: got {keys!r}")
        return 1
    if mask_key("1234567890") != "123***890":
        print(f"[FAIL] t01 mask_key long :: got {mask_key('1234567890')!r}")
        return 1
    if mask_key("abcd") != "ab***":
        print(f"[FAIL] t01 mask_key short :: got {mask_key('abcd')!r}")
        return 1
    print("[PASS] t01 normalize and mask keys")
    return 0


def t02_settings_whitelist_and_constraints() -> int:
    with tempfile.TemporaryDirectory() as d:
        eng = db.engine.DatabaseEngine(Path(d) / "t.db")
        repo = SettingsRepository(eng)

        # Check that keys exist in _EXACT_KEYS
        exact = repos._EXACT_KEYS
        for k in ("proxy.twoproxy_enabled", "proxy.twoproxy_keys", "proxy.twoproxy_live_seconds"):
            if k not in exact:
                print(f"[FAIL] t02 missing key {k} in _EXACT_KEYS")
                return 1

        # Check sensitive keys
        if "proxy.twoproxy_keys" not in repos._SENSITIVE_KEYS:
            print("[FAIL] t02 proxy.twoproxy_keys missing from _SENSITIVE_KEYS")
            return 1

        # Real set/get round-trip in SQLite DB
        repo.set("proxy.twoproxy_enabled", True)
        if repo.get("proxy.twoproxy_enabled") is not True:
            print("[FAIL] t02 proxy.twoproxy_enabled != True")
            return 1

        repo.set("proxy.twoproxy_keys", ["key_a", "key_b"])
        if repo.get("proxy.twoproxy_keys") != ["key_a", "key_b"]:
            print("[FAIL] t02 proxy.twoproxy_keys mismatch")
            return 1

        repo.set("proxy.twoproxy_live_seconds", 60)
        if repo.get("proxy.twoproxy_live_seconds") != 60:
            print("[FAIL] t02 proxy.twoproxy_live_seconds != 60")
            return 1

        # Invalid constraints should raise RepositoryError
        invalid_cases = [
            ("proxy.twoproxy_enabled", "invalid"),
            ("proxy.twoproxy_keys", "not_a_list"),
            ("proxy.twoproxy_live_seconds", 9999),
            ("proxy.twoproxy_live_seconds", -5),
        ]
        for k, v in invalid_cases:
            try:
                repo.set(k, v)
                print(f"[FAIL] t02 invalid {k}={v!r} did not raise RepositoryError")
                return 1
            except RepositoryError:
                pass
        eng.close()

    print("[PASS] t02 settings whitelist & constraints validation")
    return 0


def t03_pool_lease_and_concurrency() -> int:
    async def _test():
        pool = TwoProxyPool()
        pool.configure(enabled=True, keys=["key_1", "key_2"], live_seconds=120)

        # Mock API fetch
        async def mock_fetch(key, live_seconds=120):
            return {
                "ok": True,
                "proxy_url": f"http://proxy_{key}:8080",
                "proxy_raw": f"proxy_{key}:8080",
                "ip": f"10.0.0.{key[-1]}",
            }

        pool.fetch_proxy_from_api = mock_fetch

        # 1. Job 1 acquires key_1
        url1, k1 = await pool.acquire()
        if k1 != "key_1" or url1 != "http://proxy_key_1:8080":
            print(f"[FAIL] t03 acquire 1 :: got {k1}, {url1}")
            return 1

        # 2. Job 2 (concurrent) acquires key_2
        url2, k2 = await pool.acquire()
        if k2 != "key_2" or url2 != "http://proxy_key_2:8080":
            print(f"[FAIL] t03 acquire 2 :: got {k2}, {url2}")
            return 1

        # 3. Job 3 with no keys left -> timeout
        try:
            await pool.acquire(timeout=0.05)
            print("[FAIL] t03 acquire 3 did not timeout when all keys leased")
            return 1
        except TimeoutError:
            pass

        # 4. Job 1 finishes and releases key_1
        pool.release(k1)

        # 5. Job 3 now gets key_1 and reuses live cached IP
        url3, k3 = await pool.acquire(timeout=1.0)
        if k3 != "key_1" or url3 != "http://proxy_key_1:8080":
            print(f"[FAIL] t03 acquire 3 after release :: got {k3}, {url3}")
            return 1

        # Clean up
        pool.release(k2)
        pool.release(k3)

        st = pool.status()
        if st["leased_count"] != 0:
            print(f"[FAIL] t03 final leased_count != 0 :: got {st}")
            return 1

        print("[PASS] t03 pool lease & concurrency isolation")
        return 0

    return asyncio.run(_test())


def main() -> int:
    tests = [
        t01_normalize_and_mask,
        t02_settings_whitelist_and_constraints,
        t03_pool_lease_and_concurrency,
    ]
    failed = 0
    for t in tests:
        if t() != 0:
            failed += 1
    print(f"\n[SUMMARY] {'ALL PASS' if failed == 0 else f'{failed} FAILED'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
