"""Chẩn đoán OAuth init 403 với retry trên cùng cookie jar.

Script chỉ bootstrap NextAuth rồi GET authorize nhiều lần; không gửi mật khẩu,
OTP hoặc authorize/continue.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from request_phase import (  # noqa: E402
    _create_session,
    _navigate_oauth_authorize,
    _step_auth_url,
    _step_csrf,
)


def _cookie_names(session) -> str:
    try:
        return ", ".join(sorted({cookie.name for cookie in session.cookies.jar}))
    except Exception:
        return "<unavailable>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--impersonate", default="chrome145")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    session = _create_session(proxy=args.proxy, impersonate=args.impersonate)
    try:
        device_id = str(uuid.uuid4())
        csrf = _step_csrf(session, print)
        auth_url = _step_auth_url(
            session,
            csrf,
            print,
            device_id=device_id,
            login_hint=args.email,
        )
        response = _navigate_oauth_authorize(session, auth_url, print)
        print(
            f"status={response.status_code} landing={str(response.url)[:160]!r} "
            f"cookies={_cookie_names(session)}",
            flush=True,
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
