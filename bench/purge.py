#!/usr/bin/env python3
"""Drop invalid rows from a run so `probe.py` regenerates them on resume.

Two kinds of row are not data and must not be analysed:

  LLM_ERROR   the generation never happened (empty completion, timeout, 4xx).
              Leaving these in silently biases every rate that divides by the
              number of generations.
  a whole arm whose premise turned out to be wrong -- e.g. a library-aware arm
              run before the exact .lib line was being supplied, where failures
              measure filename guessing rather than the behaviour under test.

The original file is backed up beside the new one before anything is removed.

    python bench/purge.py <run-id> --status LLM_ERROR
    python bench/purge.py <run-id> --variant library
    python bench/purge.py <run-id> --variant library --status LLM_ERROR --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent / "runs"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id")
    parser.add_argument("--status", nargs="*", default=[],
                        help="Drop rows with these statuses, e.g. LLM_ERROR SIM_CRASH")
    parser.add_argument("--variant", nargs="*", default=[],
                        help="Drop rows from these prompt variants, e.g. library")
    parser.add_argument("--cases", nargs="*", default=[],
                        help="Drop rows for these case ids -- use when a case's "
                             "part changed and its old rows tested something else.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be dropped; change nothing.")
    args = parser.parse_args()

    if not args.status and not args.variant and not args.cases:
        parser.error("Give at least one of --status, --variant or --cases.")

    path = RUNS_DIR / args.run_id / "results.jsonl"
    if not path.exists():
        sys.exit(f"No results at {path}")

    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]

    statuses = set(args.status)
    variants = set(args.variant)
    case_ids = {c.upper() for c in args.cases}

    def drop(row: dict) -> bool:
        # Filters are ANDed, so several together target one arm's failures for
        # one case without discarding anything else.
        if statuses and row.get("status") not in statuses:
            return False
        if variants and row.get("prompt_variant", "bare") not in variants:
            return False
        if case_ids and str(row.get("case_id", "")).upper() not in case_ids:
            return False
        return True

    kept = [r for r in rows if not drop(r)]
    dropped = [r for r in rows if drop(r)]

    print(f"  rows in file : {len(rows)}")
    print(f"  to drop      : {len(dropped)}")
    for (variant, status), count in sorted(Counter(
            (r.get("prompt_variant", "bare"), r.get("status")) for r in dropped
    ).items()):
        print(f"      {variant:<9} {str(status):<12} {count}")
    print(f"  remaining    : {len(kept)}")

    if args.dry_run:
        print("\n  dry run; nothing written.")
        return 0
    if not dropped:
        print("\n  nothing matched; nothing written.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(f".jsonl.bak-{stamp}")
    shutil.copy2(path, backup)
    path.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")

    print(f"\n  backup  {backup.name}")
    print(f"  wrote   {path.name}")
    print("\n  Re-run probe.py with the same --run-id to regenerate the dropped work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
