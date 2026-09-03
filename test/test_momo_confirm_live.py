import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from curl_cffi.requests import AsyncSession
from user_agent_profile import CURL_IMPERSONATE_PRIMARY as _IMP
from payment_link import _execute_checkout_probe, _call_stripe_init_data
from momo_pay import get_momo_checkout_details

async def test_probe_momo():
    # Load last session or test if available
    print("[DEBUG] testing MoMo confirmation flow...")

if __name__ == "__main__":
    asyncio.run(test_probe_momo())
