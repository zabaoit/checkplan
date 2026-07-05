from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_mode_builds_request() -> None:
    from web.mail_modes import get_spec, serialize_for_api

    modes = {item["id"] for item in serialize_for_api()}
    assert "congas" in modes
    spec = get_spec("congas")
    parsed = spec.parse_line("worsts.flan1n@icloud.com|9DLA6F9WGHC4")
    req = spec.build_request(parsed, password="Abc123456!xx", headless=True)
    assert req.mail_provider == "congas"
    assert req.email == "worsts.flan1n@icloud.com"
    assert req.congas_rental_token == "9DLA6F9WGHC4"


async def test_provider_reads_existing_code() -> None:
    from mail_providers import build_provider_congas

    provider = build_provider_congas(
        email="worsts.flan1n@icloud.com",
        rental_token="9DLA6F9WGHC4",
    )
    code = await provider.poll_otp(
        recipient="worsts.flan1n@icloud.com",
        started_at=datetime.now(timezone.utc) - timedelta(days=2),
        timeout_seconds=20,
        poll_interval_seconds=2,
        log=lambda msg: print(msg, flush=True),
    )
    assert len(code) == 6 and code.isdigit()


def main() -> int:
    test_mode_builds_request()
    asyncio.run(test_provider_reads_existing_code())
    print("OK congas mail mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
