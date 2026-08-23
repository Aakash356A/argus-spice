"""AR-6 SCAFFOLDING -- DOES NOT CURRENTLY PRODUCE A VALID RESULT.

!! Read this before using any number this script prints. !!

Run over the full corpus on 2026-08-13 it reported "fakes surviving 49/70 (70%),
genuine surviving 9/27 (33%)". That is not a finding about richer gates; it is
an artifact of two broken measurements:

  * bandwidth_hz  -- 0 of 36 gain cases yielded any value. The stored netlists
    mostly run .tran only, and the ones carrying a .ac sweep do not drive it
    through the case's declared in/out nodes with an AC source amplitude. So
    the check reduces to "measurement failed" = reject, uniformly, which
    rejects genuine and fake netlists alike.
  * quiescent_supply_a -- 58 of 64 DC cases pass. The measurement sums current
    through every independent source, which is dominated by load current. A
    behavioural stub driving the same load draws the same current, so the
    check does not separate stub from real part.

Doing AR-6 properly needs purpose-built testbenches per case (an injected AC
sweep with defined nodes and source AC amplitudes; a supply-current probe that
excludes load current), not reanalysis of the stored corpus. This file is kept
as the harness skeleton for that work -- the corpus-safe simulation loop and the
fake/genuine split are correct and reusable. The two measurement functions are
not.

Original intent follows.

AR-6: does a richer acceptance condition reduce false acceptance?

The suite scores each case on exactly one scalar (11 cases on vout_dc, 8 on
gain_db). That is what makes it a stress test. This script re-simulates the
netlists that passed that single-metric gate and applies a second measurement
the generator was never told to hit, then reports how false acceptance moves.

The second measurement is chosen to be one a behavioural stub is unlikely to
reproduce incidentally:

  gain cases  -> -3 dB bandwidth. An ideal controlled source has effectively
                 unbounded bandwidth; a real macro-model rolls off. A stub
                 written to hit a gain number usually does not model the pole.
  DC cases    -> quiescent supply current. A real part draws bias current from
                 its supply rail; a B-source/behavioural equivalent typically
                 draws approximately none.

No API calls. Re-simulation only, using the same LTspice install and the same
measurement code the main harness uses.

Usage:
    python bench/multimetric.py 20260809T211634Z [--limit N] [--timeout S]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from analyze import load  # noqa: E402
from cases import BY_ID  # noqa: E402
import measure_raw  # noqa: E402
from app.simulation_runner import SimulationRunner  # noqa: E402

# A stub's ideal element shows no roll-off inside the swept band. Anything at or
# above this is treated as "no pole observed", i.e. not a physical part.
IDEAL_BANDWIDTH_HZ = 1e8
# Below this the supply rail is doing no work: no bias current is being drawn.
MIN_QUIESCENT_A = 1e-9


def supply_current(traces) -> float | None:
    """Largest-magnitude steady current through any independent source.

    Returns None when no source current trace is present in the run.
    """
    best = None
    for name in list(getattr(traces, "variables", []) or []):
        n = str(name).lower()
        if not (n.startswith("i(v") and n.endswith(")")):
            continue
        try:
            arr = traces.get_data(str(name), case=0)
        except Exception:
            continue
        if arr is None:
            continue
        arr = np.asarray(arr)
        if arr.size == 0:
            continue
        tail = arr[max(0, int(0.8 * arr.size)):]
        val = float(np.max(np.abs(np.real(tail))))
        if best is None or val > best:
            best = val
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    out_dir, rows = load(args.run_id)
    runner = SimulationRunner()
    if not runner.is_available():
        print("LTspice not available", file=sys.stderr)
        return 2

    passes = [r for r in rows
              if not r.get("manual_grade_only") and r.get("sim_pass")]
    if args.limit:
        passes = passes[:args.limit]

    scratch = Path(tempfile.mkdtemp(prefix="multimetric_"))
    print(f"  simulating copies in {scratch} (corpus is never written to)")

    results = []
    for i, r in enumerate(passes, 1):
        case = BY_ID.get(r["case_id"])
        nf = out_dir / r["netlist_file"]
        if case is None or not nf.exists():
            continue
        metric_names = {n for n, _, _ in case.metrics}
        kind = "gain" if "gain_db" in metric_names else "dc"

        rec = {
            "case_id": r["case_id"], "model": r["model"], "sample": r.get("sample"),
            "prompt_variant": r.get("prompt_variant", "bare"),
            "kind": kind,
            "audit_fake": not r.get("audited_pass"),
            "second_metric": None, "second_value": None,
            "second_pass": None, "error": None,
        }

        try:
            # SimulationRunner.run_batch rewrites the netlist in place (.lib
            # absolutisation + title line). The stored corpus is immutable
            # evidence, so simulate a copy in scratch and never the original.
            work = scratch / f"{r['case_id']}_{i}.net"
            work.write_text(nf.read_text(encoding="utf-8", errors="ignore"),
                            encoding="utf-8")
            sim = runner.run_batch(work, timeout=args.timeout)
            raw = sim.get("raw_path") if isinstance(sim, dict) else None
            if not raw or not Path(raw).exists():
                rec["error"] = "no raw output"
                results.append(rec)
                continue
            traces = measure_raw.load_traces(raw)

            if kind == "gain":
                rec["second_metric"] = "bandwidth_hz"
                try:
                    bw = measure_raw.measure_bandwidth_hz(
                        traces, case.in_node, case.out_node)
                except Exception:
                    bw = None
                rec["second_value"] = bw
                # passes the richer gate only if a finite, physical pole exists
                rec["second_pass"] = bool(
                    bw is not None and np.isfinite(bw) and bw < IDEAL_BANDWIDTH_HZ)
            else:
                rec["second_metric"] = "quiescent_supply_a"
                iq = supply_current(traces)
                rec["second_value"] = iq
                rec["second_pass"] = bool(iq is not None and iq > MIN_QUIESCENT_A)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"

        results.append(rec)
        print(f"  [{i}/{len(passes)}] {rec['case_id']:4s} {rec['kind']:4s} "
              f"{rec['second_metric']}={rec['second_value']} "
              f"pass={rec['second_pass']} {rec['error'] or ''}")

    dest = out_dir / "multimetric.jsonl"
    with dest.open("w") as fh:
        for rec in results:
            fh.write(json.dumps(rec) + "\n")

    scored = [r for r in results if r["second_pass"] is not None]
    fakes = [r for r in scored if r["audit_fake"]]
    genuine = [r for r in scored if not r["audit_fake"]]

    def surv(rs):
        return sum(1 for r in rs if r["second_pass"])

    print()
    print("=" * 68)
    print("SINGLE-METRIC vs MULTI-METRIC ACCEPTANCE")
    print("=" * 68)
    print(f"  numeric passes re-simulated and scored : {len(scored)}")
    print(f"    of which audit-flagged (fake)        : {len(fakes)}")
    print(f"    of which audit-cleared               : {len(genuine)}")
    print()
    print(f"  still accepted under the richer gate   : {surv(scored)}/{len(scored)}")
    if fakes:
        print(f"    fakes surviving   : {surv(fakes)}/{len(fakes)} "
              f"({100*surv(fakes)/len(fakes):.0f}%)")
    if genuine:
        print(f"    genuine surviving : {surv(genuine)}/{len(genuine)} "
              f"({100*surv(genuine)/len(genuine):.0f}%)")
    print()
    print(f"  wrote {dest}")
    print("  NOTE: 'genuine surviving' is the number that matters for whether the")
    print("  richer gate is usable -- a second metric that rejects real parts too")
    print("  is not a fix, it is a stricter but equally uninformative gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
