#!/usr/bin/env python3
"""Pin the vendor-library snapshot the paper's provenance results rest on.

    python bench/snapshot_library.py 20260809T211634Z

Writes `data/library_snapshot.json`: the library-relative path and SHA-256 of
every library file that a corpus netlist or a reference template resolves to on
this machine, and the simulator version LTspice reports for itself. The models
themselves are not redistributed; only their digests are.

What this establishes: a reader with a different library sees a digest mismatch
instead of a silent disagreement, and a deck that binds a file changed after
the snapshot is caught. What it does not: the digests are of one installation
as downloaded from the vendor, not values the vendor publishes, and because the
corpus itself is an input, no unmodified corpus deck can fail the match. A
production deployment would pin the approved release instead.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import provenance  # noqa: E402
from app.simulation_runner import simulator_version  # noqa: E402
from provenance import _normalise_path, resolve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    args = ap.parse_args()
    run = ROOT / "bench" / "runs" / args.run_id

    texts = [p.read_text(errors="ignore") for p in sorted((run / "netlists").glob("*.net"))]
    texts += [p.read_text(errors="ignore")
              for p in sorted((ROOT / "data" / "templates").glob("*.net"))]

    files: dict[str, str] = {}
    for text in texts:
        # The part name is irrelevant to which files resolve; any string works.
        for f in resolve(text, "-", snapshot={}).files:
            if f.path and f.sha256:
                files[_normalise_path(f.path)] = f.sha256

    out = ROOT / "data" / "library_snapshot.json"
    out.write_text(json.dumps({"simulator": simulator_version() or "unknown",
                               "files": dict(sorted(files.items()))},
                              indent=1) + "\n")
    print(f"pinned {len(files)} library files -> {out.relative_to(ROOT)}")
    provenance._snapshot_cache = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
