"""Block until every rollup in config/rollup_cases.yml is built and serving.

A query matched to a rollup that has not been materialised yet fails or falls back, so running
the rollup tier straight after a deploy or a dbt run gives false failures. Run this first.

Usage:  python scripts/wait_for_rollups.py [--timeout 1800]
Exit code 0 when all rollups serve; 1 on timeout.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from semtest import cases, config  # noqa: E402
from semtest.cube import CubeClient, CubeError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    config.load_dotenv()
    cube = CubeClient(config.env("CUBE_API_URL"), config.env("CUBE_API_SECRET"), timeout=120)
    personas = config.personas()
    pending = {c["id"]: cases.prepare(c) for c in config.rollup_cases()}
    deadline = time.time() + args.timeout

    while pending and time.time() < deadline:
        for cid, case in list(pending.items()):
            try:
                resp = cube.load(case["cube_query"], personas[case["persona"]]["claims"])
            except CubeError as exc:
                print(f"waiting: {cid}: {str(exc).splitlines()[0][:160]}")
                continue
            if case["pre_aggregation"] in json.dumps(resp.get("usedPreAggregations") or {}):
                print(f"ready:   {cid}")
                del pending[cid]
            else:
                print(f"waiting: {cid}: answered, but not from {case['pre_aggregation']} yet")
        if pending:
            time.sleep(args.interval)

    if pending:
        print(f"timed out; still not serving: {sorted(pending)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
