import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_held_sessions = []

async def test_keep_open():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    profile_dir = _ROOT / "runtime" / "profiles" / "test_keep_edge"
    profile_dir.mkdir(parents=True, exist_ok=True)
    
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        channel="msedge",
        args=["--start-maximized"],
    )
    _held_sessions.append((pw, context))
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://chatgpt.com/", timeout=20000)
    print("[PASS] Browser opened and staying open for 5 seconds...")
    await asyncio.sleep(5)
    await context.close()
    await pw.stop()

if __name__ == "__main__":
    asyncio.run(test_keep_open())
