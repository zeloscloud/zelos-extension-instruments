"""Record a transcript from every instrument in `BENCH` (see test_bench.py)."""

import json
import os
import sys

from zelos_extension_instruments import record


def main() -> int:
    bench = json.loads(os.environ.get("BENCH", "[]"))
    failed = 0
    for entry in bench:
        code = record.main([entry["profile"], entry["address"], "--set"])
        failed += code != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
