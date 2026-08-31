"""Offline checks cho retry policy của change_password API flow."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import change_password as target  # noqa: E402


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""


class _Session:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = iter(statuses)
        self.calls = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response(next(self.statuses))


async def _check_warm_retry() -> None:
    session = _Session([403, 200])
    response = await target._get_with_warm_retry(
        session,
        "https://chatgpt.com/api/auth/session",
        headers={},
        log=lambda _message: None,
        label="session",
    )
    assert response.status_code == 200
    assert session.calls == 2


async def _check_wrapper_retry_and_commit_guard() -> None:
    original_once = target._change_password_once
    original_sleep = target.asyncio.sleep
    target.asyncio.sleep = lambda _delay: _no_sleep()
    try:
        attempts = 0

        async def retry_then_success(**_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise target.ChangePasswordError("login HTTP 403", retryable=True)
            return {"ok": True}

        target._change_password_once = retry_then_success
        result = await target.change_password(
            email="a@example.com",
            current_password="old",
            new_password="new",
        )
        assert result == {"ok": True}
        assert attempts == 3

        committed_attempts = 0

        async def committed_failure(**_kwargs):
            nonlocal committed_attempts
            committed_attempts += 1
            raise target.ChangePasswordError(
                "refreshed session HTTP 403",
                retryable=True,
                committed=True,
            )

        target._change_password_once = committed_failure
        try:
            await target.change_password(
                email="a@example.com",
                current_password="old",
                new_password="new",
            )
        except target.ChangePasswordError:
            pass
        else:
            raise AssertionError("committed flow should fail without retry")
        assert committed_attempts == 1
    finally:
        target._change_password_once = original_once
        target.asyncio.sleep = original_sleep


async def _no_sleep() -> None:
    return None


def main() -> int:
    asyncio.run(_check_warm_retry())
    asyncio.run(_check_wrapper_retry_and_commit_guard())
    print("[PASS] change password stability policy", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
