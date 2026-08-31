"""Verify OAuth authorize warm-retry không cần network thật."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from request_phase import (  # noqa: E402
    RequestPhaseError,
    _is_cloudflare_block_error,
    _navigate_oauth_authorize,
)


class _Response:
    def __init__(self, status_code: int, url: str = "https://auth.openai.com/log-in/password"):
        self.status_code = status_code
        self.url = url


class _Session:
    def __init__(self, statuses: list[int]):
        self._statuses = iter(statuses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response(next(self._statuses))


def _check_warm_retry_success() -> None:
    session = _Session([403, 200])
    logs: list[str] = []
    response = _navigate_oauth_authorize(session, "https://auth.openai.com/authorize", logs.append)
    assert response.status_code == 200
    assert session.calls == 2
    assert any("warm-retry cùng session" in line for line in logs)


def _check_403_exhaustion() -> None:
    session = _Session([403, 403, 403])
    try:
        _navigate_oauth_authorize(session, "https://auth.openai.com/authorize", lambda _msg: None)
    except RequestPhaseError as exc:
        assert str(exc) == "OAuth init failed: HTTP 403"
        assert _is_cloudflare_block_error(exc)
    else:
        raise AssertionError("expected RequestPhaseError after 3 HTTP 403 responses")
    assert session.calls == 3


def _check_non_403_fail_fast() -> None:
    session = _Session([500, 200])
    try:
        _navigate_oauth_authorize(session, "https://auth.openai.com/authorize", lambda _msg: None)
    except RequestPhaseError as exc:
        assert str(exc) == "OAuth init failed: HTTP 500"
    else:
        raise AssertionError("expected RequestPhaseError for HTTP 500")
    assert session.calls == 1


def main() -> int:
    _check_warm_retry_success()
    _check_403_exhaustion()
    _check_non_403_fail_fast()
    print("[PASS] OAuth init warm-retry + fail-fast", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
