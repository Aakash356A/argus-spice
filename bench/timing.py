#!/usr/bin/env python3
"""Static resolution time over every returned deck, in one fresh process.

    python bench/timing.py 20260809T211634Z

Each library file is parsed and hashed once per process and cached, so the
tail is the first visit to a large file. Operating-system file caches are left
as they are; this is not a cold-disk measurement.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

from analyze import load                                  # noqa: E402
from cases import BY_ID                                   # noqa: E402
from provenance import resolve                            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--out", default="bench/runs/timing.json")
    args = ap.parse_args()
    out_dir, rows = load(args.run_id)
    times = []
    for r in rows:
        if not r.get("netlist_file") or r.get("manual_grade_only"):
            continue
        text = (out_dir / r["netlist_file"]).read_text(errors="ignore")
        t0 = time.perf_counter()
        resolve(text, BY_ID[r["case_id"]].part)
        times.append(1e3 * (time.perf_counter() - t0))
    s = sorted(times)
    rec = {"decks": len(s), "median_ms": s[len(s) // 2],
           "p95_ms": s[int(0.95 * len(s))], "max_ms": s[-1], "total_s": sum(s) / 1e3}
    Path(args.out).write_text(json.dumps(rec, indent=1) + "\n")
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
