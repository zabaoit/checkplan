from __future__ import annotations

import sys
import time
import urllib.request


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8083/"
    last_error: Exception | None = None
    for _ in range(20):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                body = resp.read(4096).decode("utf-8", errors="replace")
                if resp.status == 200 and ("UPI" in body or "html" in body.lower()):
                    print(f"OK {url} status={resp.status}")
                    return 0
                print(f"Unexpected response status={resp.status}")
                return 1
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)

    print(f"Web smoke failed: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
