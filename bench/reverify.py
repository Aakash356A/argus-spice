"""Mechanical re-verification of the judgement-dependent grades.

Every numeric pass whose part name resolves to a vendor definition was graded
by reading the netlist. This script replaces reading with three measurements
on each one, so those grades rest on something a reader can re-run:

  delete    remove every instance of the requested part, re-simulate with the
            identical testbench, and re-score. If the target is still met,
            the part was not producing the measured response.
  thd       total harmonic distortion of the output, measured two ways: the
            FFT used by waveform.py, and a least-squares harmonic fit on
            LTspice's raw, adaptively spaced samples, which needs neither
            resampling nor a window. Gain cases only.
  respond   for cases with a defined input-side perturbation, move the input
            across its threshold and require the output to follow. A part
            that is dead or miswired holds its output still.

A control re-simulation of the unmodified netlist comes first; a row whose
control no longer passes is reported, not scored.

Usage:
    python bench/reverify.py 20260809T211634Z
"""
from __future__ import annotations

import argparse
import json
import math
import logging
import re
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

from ablation import predicate_values                       # noqa: E402
from analyze import load                                    # noqa: E402
from app.simulation_runner import SimulationRunner          # noqa: E402
from cases import BY_ID                                     # noqa: E402
from detect import _same_part, _x_subckt_index              # noqa: E402
from measure_raw import _get_trace, load_traces             # noqa: E402
from sim_harness import Metric, Spec, run_spice             # noqa: E402
from spec_report import report as build_report              # noqa: E402
from waveform import THD_LIMIT, _raw_after_run, _waveform_metrics  # noqa: E402

# Input-side perturbations, by case. Each moves a DC input across the
# threshold the specification defines and states which way the output must go.
RESPONSE_TESTS = {
    # Comparator with a 1.65 V threshold; the spec holds the input above it,
    # so the output is low, and dropping the input below must drive it high.
    "D1": {"node": "IN", "value": 0.5, "min_swing_v": 2.0, "direction": "up"},
}


def score(netlist, case):
    sim = run_spice(netlist, testbench="", timeout_s=120, in_node=case.in_node,
                    out_node=case.out_node, freq_hz=case.freq_hz,
                    requested_measurements={m[0] for m in case.metrics})
    if not sim.converged:
        return "no_sim", None
    reps = build_report(sim, Spec(metrics=[Metric(n, t, tol, "hit_target")
                                           for n, t, tol in case.metrics], testbench=""))
    if not reps or any(r.measured is None or not math.isfinite(float(r.measured))
                       for r in reps):
        diag = " ".join(sim.diagnostics).lower()
        # A transient gain of log10(0): the output carries no signal at all.
        return ("zero_output" if "math domain" in diag else "no_measure"), None
    return ("met" if all(r.passed for r in reps) else "lost"), float(reps[0].measured)


def deletion_outcome(status: str) -> str:
    """Explicit outcome of re-simulating with the part deleted."""
    return {"met": "still_met", "lost": "finite_miss", "zero_output": "zero_output",
            "no_measure": "no_measure", "no_sim": "no_sim"}[status]


def _clean(x):
    """JSON-safe: non-finite floats become null rather than NaN."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    return x


def delete_part(netlist, part):
    out, removed = [], 0
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line[:1].upper() == "X":
            tokens = line.split()
            i = _x_subckt_index(tokens)
            if i is not None and _same_part(tokens[i], part):
                out.append("* [reverify] removed: " + raw)
                removed += 1
                continue
        out.append(raw)
    return "\n".join(out) + "\n", removed


def ls_thd(t, y, f, harmonics=10):
    w = 2 * np.pi * f
    cols = [np.ones_like(t)]
    for k in range(1, harmonics + 1):
        cols += [np.sin(k * w * t), np.cos(k * w * t)]
    coef, *_ = np.linalg.lstsq(np.stack(cols, 1), y, rcond=None)
    amp = [np.hypot(coef[2 * k - 1], coef[2 * k]) for k in range(1, harmonics + 1)]
    return float(np.sqrt(sum(a * a for a in amp[1:])) / amp[0])


def thd_both(netlist, case):
    runner = SimulationRunner()
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="reverify-") as d:
            path = Path(d) / "c.net"
            path.write_text(netlist)
            started = time.monotonic()
            result = runner.run_batch(path, timeout=120)
            raw = _raw_after_run(result, path, 120 - (time.monotonic() - started))
            if raw is None:
                continue
            traces = load_traces(raw)
            t = np.asarray(traces.get_time(case=0), float)
            y = np.asarray(_get_trace(traces, case.out_node, case=0))
            y = y.real if np.iscomplexobj(y) else y.astype(float)
            fft = _waveform_metrics(t, y, case.freq_hz)
            half = len(t) // 2
            return (fft["thd"] if fft else None), ls_thd(t[half:], y[half:], case.freq_hz)
    return None, None


def respond(netlist, case, test):
    pattern = rf'(?im)^(\s*V\S*\s+{re.escape(test["node"])}\s+0\s+)(?:DC\s+)?[-+\d.eE]+\s*V?\s*$'
    flipped, n = re.subn(pattern, rf'\g<1>{test["value"]}', netlist)
    if not n:
        return None
    _, before = score(netlist, case)
    _, after = score(flipped, case)
    if before is None or after is None:
        return None
    up = after > before
    return {"before": before, "after": after,
            "responds": abs(after - before) >= test["min_swing_v"],
            "direction_ok": up if test.get("direction") == "up" else not up}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    args = ap.parse_args()
    out_dir, rows = load(args.run_id)
    records = []
    for r in rows:
        arm = r.get("prompt_variant", "bare")
        if arm not in ("bare", "library", "requirement") or not r.get("sim_pass"):
            continue
        netlist = (out_dir / r["netlist_file"]).read_text(errors="ignore")
        if predicate_values(r, netlist)["resolved_binding"]:
            continue                      # provenance already settles these
        case = BY_ID[r["case_id"]]
        rec = {"case_id": r["case_id"], "model": r["model"], "sample": r["sample"],
               "prompt_variant": arm, "netlist_file": r["netlist_file"]}
        rec["control"], rec["control_value"] = score(netlist, case)
        if rec["control"] == "met":
            ablated, rec["instances_removed"] = delete_part(netlist, case.part)
            rec["delete"], rec["delete_value"] = score(ablated, case)
            rec["delete_outcome"] = deletion_outcome(rec["delete"])
            rec["part_necessary"] = rec["delete"] != "met"
            if case.metrics[0][0] == "gain_db":
                rec["thd_fft"], rec["thd_lsq"] = thd_both(netlist, case)
                rec["distorted"] = (rec["thd_fft"] is not None
                                    and rec["thd_fft"] > THD_LIMIT)
            if case.case_id in RESPONSE_TESTS:
                rec["response"] = respond(netlist, case, RESPONSE_TESTS[case.case_id])
        records.append(rec)
        print(f"  {Path(r['netlist_file']).name:<50} control={rec['control']:<9}"
              f" necessary={rec.get('part_necessary')} distorted={rec.get('distorted')}"
              f" responds={(rec.get('response') or {}).get('responds')}")
    dest = out_dir / "reverification.jsonl"
    dest.write_text("\n".join(json.dumps(_clean(x)) for x in records) + "\n")
    print(f"\nwrote {dest} ({len(records)} provenance-qualified passes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
