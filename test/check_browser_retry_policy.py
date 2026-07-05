from __future__ import annotations

import py_compile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _browser_retry


def main() -> None:
    if _browser_retry.LAUNCH_RETRY_MAX != 4:
        raise AssertionError(f"LAUNCH_RETRY_MAX={_browser_retry.LAUNCH_RETRY_MAX}, expected 4")

    sample = Exception("Page.goto: Connection closed while reading from the driver")
    if not _browser_retry.is_driver_dead_error(sample):
        raise AssertionError("driver-dead marker was not detected")

    for rel in ("_browser_retry.py", "browser_phase.py", "session_phase.py"):
        py_compile.compile(str(ROOT / rel), doraise=True)

    print("OK browser retry policy")


if __name__ == "__main__":
    main()
