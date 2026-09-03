import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

async def test_launch_with_cookies():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    
    profile_dir = _ROOT / "runtime" / "profiles" / "test_session_edge"
    profile_dir.mkdir(parents=True, exist_ok=True)

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        channel="msedge",
        args=["--start-maximized"],
    )

    # Add mock cookie
    await context.add_cookies([
        {"name": "test_cookie", "value": "123", "domain": ".chatgpt.com", "path": "/"}
    ])

    page = context.pages[0] if context.pages else await context.new_page()
    print("[PASS] Browser with cookies launched!")
    await context.close()
    await pw.stop()

if __name__ == "__main__":
    asyncio.run(test_launch_with_cookies())
