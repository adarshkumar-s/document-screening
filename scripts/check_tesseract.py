"""Dependency-free Tesseract version diagnostic.

Adapted from the diagnostic helper used by GP-HUE/land_records v3.9.8.
Usage: python scripts/check_tesseract.py <path-to-tesseract>
Returns 0 when Tesseract is >= 5.5, otherwise 1.
"""
from __future__ import annotations

import re
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check_tesseract.py <tesseract-exe>")
        return 1

    path = sys.argv[1]
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"could not run tesseract: {exc}")
        return 1

    output = (proc.stdout or "") + (proc.stderr or "")
    lines = [line for line in output.splitlines() if line.strip()]
    first = lines[0] if lines else "tesseract (no version output)"
    print(first)

    match = re.search(r"v?(\d+)\.(\d+)", first)
    if not match:
        print("version check: COULD NOT PARSE")
        return 1

    major, minor = int(match.group(1)), int(match.group(2))
    ok = major > 5 or (major == 5 and minor >= 5)
    print("version check: " + ("OK (>= 5.5)" if ok else "TOO OLD (need >= 5.5)"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
