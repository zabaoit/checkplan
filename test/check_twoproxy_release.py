import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import AsyncMock, patch
from web.twoproxy_pool import TwoProxyPool

async def main():
    pool = TwoProxyPool()
    pool.configure(enabled=True, keys=["TEST_KEY_123"], live_seconds=120)
    
    call_count = 0
    async def mock_fetch(key, live_seconds=120):
        nonlocal call_count
        call_count += 1
        return {
            "ok": True,
            "proxy_url": f"http://proxy_{call_count}.com:8080",
            "proxy_raw": f"proxy_{call_count}.com:8080",
            "ip": f"1.1.1.{call_count}",
        }
    
    with patch.object(pool, "fetch_proxy_from_api", side_effect=mock_fetch):
        # Job 1 acquires proxy
        proxy1, key1 = await pool.acquire()
        assert proxy1 == "http://proxy_1.com:8080"
        assert call_count == 1
        
        # Job 1 finishes -> releases key (clear_cache=True)
        pool.release(key1)
        assert "TEST_KEY_123" not in pool._cache
        
        # Job 2 acquires proxy -> should fetch fresh IP (not reuse cached)
        proxy2, key2 = await pool.acquire()
        assert proxy2 == "http://proxy_2.com:8080"
        assert call_count == 2
        
        print("PASS: TwoProxyPool release correctly clears cache and forces fresh IP rotation!")

if __name__ == "__main__":
    asyncio.run(main())
