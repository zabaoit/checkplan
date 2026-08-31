import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web.change_password_manager import ChangePasswordJobManager

async def main():
    mgr = ChangePasswordJobManager()
    combos = [
        "test1@example.com|pass123|OLDSECRET123",
        "test2@example.com|pass456",
    ]
    jobs = mgr.add_jobs(combos, change_2fa_only=True)
    assert len(jobs) == 2, f"Expected 2 jobs, got {len(jobs)}"
    for j in jobs:
        assert j.change_2fa_only is True
        assert j.mode == "change_2fa"
        assert j.new_password is None, f"Expected None new_password for change_2fa, got {j.new_password}"
    await mgr.stop_all()
    mgr.shutdown()
    print("[PASS] Change 2FA manager integration verified.")

if __name__ == "__main__":
    asyncio.run(main())
