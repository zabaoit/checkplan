from __future__ import annotations

import json

import requests


URL = (
    "https://mail.congas.uk/api/rentals/inbox"
    "?email=worsts.flan1n%40icloud.com&rentalToken=9DLA6F9WGHC4"
)


def main() -> int:
    resp = requests.get(URL, timeout=20)
    print("status", resp.status_code)
    print(resp.text[:4000])
    try:
        data = resp.json()
    except ValueError:
        return 0
    print("keys", sorted(data.keys()) if isinstance(data, dict) else type(data).__name__)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
