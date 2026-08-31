"""Verify password reset hỗ trợ MFA trước và sau bước gửi mật khẩu mới."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import change_password as target  # noqa: E402


PASSWORD_VERIFY = "https://auth.openai.com/api/accounts/password/verify"
PASSWORD_RESET = "https://auth.openai.com/api/accounts/password/reset"
MFA_ISSUE = "https://auth.openai.com/api/accounts/mfa/issue_challenge"
MFA_VERIFY = "https://auth.openai.com/api/accounts/mfa/verify"
CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=test"


class _Cookies:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], str] = {}

    def set(self, name: str, value: str, *, domain: str, path: str) -> None:
        self.values[(name, domain, path)] = value


class _Response:
    def __init__(
        self,
        status_code: int,
        data: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._data = data or {}
        self.text = json.dumps(self._data)
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._data


class _Session:
    def __init__(self, post_responses: list[tuple[str, _Response]]) -> None:
        self._post_responses = list(post_responses)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.cookies = _Cookies()

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", url, kwargs.get("json")))
        expected_url, response = self._post_responses.pop(0)
        assert url == expected_url, f"expected POST {expected_url}, got {url}"
        return response

    async def get(self, url: str, **_kwargs: Any) -> _Response:
        self.calls.append(("GET", url, None))
        assert url == CALLBACK
        return _Response(302)


def _mfa_data(challenge_id: str) -> dict[str, Any]:
    return {
        "page": {
            "type": "mfa_challenge",
            "payload": {
                "factors": [
                    {"factor_type": "totp", "id": challenge_id},
                ],
            },
        },
        "continue_url": f"https://auth.openai.com/mfa-challenge/{challenge_id}",
    }


async def _run_pre_reset_mfa() -> None:
    session = _Session([
        (PASSWORD_VERIFY, _Response(200, _mfa_data("pre_mfa"))),
        (MFA_ISSUE, _Response(200)),
        (
            MFA_VERIFY,
            _Response(200, {
                "page": {"type": "reset_password"},
                "continue_url": "https://auth.openai.com/reset-password/new-password",
            }),
        ),
        (PASSWORD_RESET, _Response(200, {"continue_url": CALLBACK})),
    ])
    await target.change_password_after_reauth(
        session,
        current_password="old-password",
        new_password="new-password",
        secret="TEST-SECRET",
        log=lambda _message: None,
    )
    post_urls = [url for method, url, _body in session.calls if method == "POST"]
    assert post_urls == [PASSWORD_VERIFY, MFA_ISSUE, MFA_VERIFY, PASSWORD_RESET]


async def _run_post_reset_mfa() -> None:
    session = _Session([
        (
            PASSWORD_VERIFY,
            _Response(200, {
                "page": {"type": "reset_password"},
                "continue_url": "https://auth.openai.com/reset-password/new-password",
            }),
        ),
        (PASSWORD_RESET, _Response(200, _mfa_data("post_mfa"))),
        (MFA_ISSUE, _Response(200)),
        (MFA_VERIFY, _Response(200, {"continue_url": CALLBACK})),
    ])
    await target.change_password_after_reauth(
        session,
        current_password="old-password",
        new_password="new-password",
        secret="TEST-SECRET",
        log=lambda _message: None,
    )
    post_urls = [url for method, url, _body in session.calls if method == "POST"]
    assert post_urls == [PASSWORD_VERIFY, PASSWORD_RESET, MFA_ISSUE, MFA_VERIFY]


def main() -> int:
    target.time_remaining = lambda: 30.0
    target.generate_code = lambda _secret: "123456"
    asyncio.run(_run_pre_reset_mfa())
    asyncio.run(_run_post_reset_mfa())
    print("[PASS] change password MFA ordering", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
