#!/usr/bin/env python3
"""Evaluator-owned fixtures: re-test frozen numeric passes under our own stimulus.

    python bench/fixtures.py 20260809T211634Z

The original experiment scores each deck under the testbench its generator
wrote. Here the evaluator takes over the input: the deck's own source on
`IN` and its analysis and measurement directives are removed, and a fixed
stimulus, analysis, observation window and measurement are imposed. Every
other element of the deck -- the circuit, its supplies, loads and any local
subcircuit -- is left exactly as generated. Each case adds properties beyond
the original scalar, chosen and thresholded before any deck was run
(`FIXTURES` below is the pre-registration).

The question is whether a functional test the evaluator controls separates
decks that use the vendor model from decks that substitute a local
definition. A deck the fixture cannot take over (its `IN` is not driven by a
single independent source to ground) is reported as unsupported, not dropped.

Measurements are taken from the raw waveform with compression disabled. Gain,
phase and THD come from a least-squares fit of a DC term and harmonics 1-5 at
the stimulus frequency over the final ten periods, on LTspice's own samples.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

from analyze import load                                  # noqa: E402
from cases import BY_ID                                   # noqa: E402
from provenance import resolve                            # noqa: E402

# ---------------------------------------------------------------- pre-registration
# Thresholds are fixed here, before any deck is simulated, and are not tuned.
FIXTURES = {
    "A4": {  # ADR1399H reference from a 15 V supply
        "kind": "dc_line",
        "supply": 15.0, "step_to": 16.5, "t_step": 50e-3, "t_end": 100e-3,
        "target": 7.05, "tol": 0.05,
        "settle_tol": 2e-3,          # |mean(45-50ms) - mean(40-45ms)|
        "line_tol": 0.035,           # |dVout| for +10% supply (0.5 %)
    },
    "B1": {  # AD8092, gain 5 V/V non-inverting, 100 mV at 1 MHz
        "kind": "gain", "amp": 0.1, "freq": 1e6, "periods": 20,
        "gain_db": 13.98, "tol_db": 1.0, "phase_tol_deg": 45.0, "thd_max": 0.01,
    },
    "B4": {  # AD8551, gain 100 V/V non-inverting, 10 mV at 1 kHz
        "kind": "gain", "amp": 0.01, "freq": 1e3, "periods": 20,
        "gain_db": 40.0, "tol_db": 1.0, "phase_tol_deg": 45.0, "thd_max": 0.01,
    },
    "D1": {  # LT1720 comparator, 1.65 V threshold, output low above it
        "kind": "comparator", "above": 2.5, "below": 0.8,
        "low_max": 0.3, "high_min": 4.0, "trip": 1.65, "trip_tol": 0.15,
    },
}

_ANALYSIS = re.compile(r"^\s*\.(tran|ac|dc|op|noise|tf|four|meas|measure|step|save|probe|wave)\b",
                       re.IGNORECASE)


def take_over(deck: str) -> tuple[str | None, dict]:
    """Remove the deck's own input source and analyses. Returns the body and
    what was found, or None when the input cannot be taken over."""
    lines = deck.splitlines()
    body, info, depth = [], {"in_sources": []}, 0
    for raw in lines:
        line = raw.split(";", 1)[0].strip()
        low = line.lower()
        if low.startswith(".end") and not low.startswith(".ends"):
            break
        if low.startswith(".subckt"):
            depth += 1
        elif low.startswith(".ends"):
            depth = max(0, depth - 1)
        if depth == 0 and _ANALYSIS.match(line):
            continue
        tokens = line.split()
        if depth == 0 and tokens and tokens[0][:1].upper() in "VI" and len(tokens) >= 3:
            nodes = [tokens[1].upper(), tokens[2].upper()]
            if "IN" in nodes:
                info["in_sources"].append(line)
                continue
        body.append(raw)
    srcs = info["in_sources"]
    if len(srcs) != 1:
        return None, {**info, "reason": f"{len(srcs)} sources on IN"}
    tok = srcs[0].split()
    if tok[0][:1].upper() != "V" or tok[1].upper() != "IN" or tok[2] not in ("0", "GND", "gnd"):
        return None, {**info, "reason": "IN not driven by one voltage source to ground"}
    info["offset"] = _dc_offset(srcs[0])
    return "\n".join(body), info


def _num(tok: str) -> float | None:
    m = re.match(r"^([-+]?\d*\.?\d+(?:e[-+]?\d+)?)(meg|[fpnumkgt])?v?$", tok.strip(), re.I)
    if not m:
        return None
    scale = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3,
             "meg": 1e6, "g": 1e9, "t": 1e12}
    return float(m.group(1)) * scale.get((m.group(2) or "").lower(), 1.0)


def _dc_offset(src: str) -> float:
    """Bias point of the deck's own input source (SINE offset or DC value)."""
    m = re.search(r"sine\s*\(\s*([^\s)]+)", src, re.I)
    if m and _num(m.group(1)) is not None:
        return _num(m.group(1))
    m = re.search(r"\bdc\s+([^\s]+)", src, re.I)
    if m and _num(m.group(1)) is not None:
        return _num(m.group(1))
    tok = src.split()
    if len(tok) >= 4 and _num(tok[3]) is not None:
        return _num(tok[3])
    return 0.0


def stimulus(case_id: str, offset: float) -> str:
    f = FIXTURES[case_id]
    if f["kind"] == "dc_line":
        t = f["t_step"]
        return (f"VFIX IN 0 PWL(0 {f['supply']} {t} {f['supply']} {t*1.001} {f['step_to']})\n"
                f".tran 0 {f['t_end']} 0 {f['t_end']/2000}\n")
    if f["kind"] == "gain":
        per = 1.0 / f["freq"]
        t_end = f["periods"] * per
        return (f"VFIX IN 0 SINE({offset} {f['amp']} {f['freq']})\n"
                f".tran 0 {t_end} 0 {per/200}\n")
    if f["kind"] == "comparator":
        a, b = f["above"], f["below"]
        return (f"VFIX IN 0 PWL(0 {a} 200u {a} 201u {b} 400u {b} 1400u {a})\n"
                f".tran 0 1400u 0 0.2u\n")
    raise ValueError(case_id)


def simulate(deck: str, timeout_s: int = 180):
    from app.simulation_runner import SimulationRunner
    import measure_raw
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.net"
        path.write_text(deck)
        res = SimulationRunner().run_batch(path, timeout=timeout_s)
        if not res.get("raw_path"):
            log = ""
            if res.get("log_path"):
                log = Path(res["log_path"]).read_text(errors="ignore").replace("\x00", "")
            fatal = next((l.strip() for l in log.splitlines() if "rror" in l), "")
            return None, (fatal or str(res.get("error", "")))[:160]
        obj = measure_raw.load_traces(res["raw_path"])
        t = np.abs(np.asarray(obj.get_time(), dtype=float))
        out = {}
        for name in ("V(out)", "V(in)"):
            d = obj.get_data(name)
            if d is None:
                return None, f"no trace {name}"
            out[name.lower()] = np.asarray(d, dtype=float)
        return (t, out["v(in)"], out["v(out)"]), ""


def _mean(t, v, a, b):
    sel = (t >= a) & (t <= b)
    return float(np.mean(v[sel])) if sel.any() else math.nan


def _fit(t, v, f, harmonics=5):
    cols = [np.ones_like(t)]
    for h in range(1, harmonics + 1):
        cols += [np.cos(2 * np.pi * h * f * t), np.sin(2 * np.pi * h * f * t)]
    coef, *_ = np.linalg.lstsq(np.stack(cols, 1), v, rcond=None)
    amps = [math.hypot(coef[2*h-1], coef[2*h]) for h in range(1, harmonics + 1)]
    phase = math.atan2(-coef[2], coef[1])          # of the fundamental
    return amps, phase


def score(case_id: str, wave) -> dict:
    f = FIXTURES[case_id]
    t, vin, vout = wave
    if f["kind"] == "dc_line":
        ts = f["t_step"]
        v1 = _mean(t, vout, 0.8 * ts, 0.9 * ts)
        v2 = _mean(t, vout, 0.9 * ts, ts)
        v3 = _mean(t, vout, 0.9 * f["t_end"], f["t_end"])
        p1 = abs(v2 - f["target"]) <= f["tol"] and abs(v2 - v1) <= f["settle_tol"]
        p2 = abs(v3 - v2) <= f["line_tol"]
        return {"vout": v2, "settle_delta": v2 - v1, "line_delta": v3 - v2,
                "p1": p1, "p2": p2, "all": p1 and p2}
    if f["kind"] == "gain":
        per = 1.0 / f["freq"]
        sel = t >= (f["periods"] - 10) * per
        ai, phi = _fit(t[sel], vin[sel], f["freq"])
        ao, pho = _fit(t[sel], vout[sel], f["freq"])
        gain_db = 20 * math.log10(ao[0] / ai[0]) if ai[0] > 0 and ao[0] > 0 else -math.inf
        dphi = math.degrees((pho - phi + math.pi) % (2 * math.pi) - math.pi)
        thd = math.sqrt(sum(a * a for a in ao[1:])) / ao[0] if ao[0] > 0 else math.inf
        p1 = abs(gain_db - f["gain_db"]) <= f["tol_db"]
        p2 = abs(dphi) <= f["phase_tol_deg"] and thd <= f["thd_max"]
        return {"gain_db": gain_db, "phase_deg": dphi, "thd": thd,
                "p1": p1, "p2": p2, "all": p1 and p2}
    if f["kind"] == "comparator":
        lo = _mean(t, vout, 150e-6, 200e-6)
        hi = _mean(t, vout, 350e-6, 400e-6)
        ramp = (t >= 400e-6)
        tr, vr, orr = t[ramp], vin[ramp], vout[ramp]
        mid = (lo + hi) / 2 if math.isfinite(lo) and math.isfinite(hi) else 2.5
        idx = np.where((orr[:-1] > mid) & (orr[1:] <= mid))[0]
        trip = float(vr[idx[0]]) if len(idx) else math.nan
        p1 = lo <= f["low_max"]
        p2 = hi >= f["high_min"]
        p3 = math.isfinite(trip) and abs(trip - f["trip"]) <= f["trip_tol"]
        return {"v_above": lo, "v_below": hi, "trip_v": trip,
                "p1": p1, "p2": p2, "p3": p3, "all": p1 and p2 and p3}
    raise ValueError(case_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--out", default="bench/runs/fixtures")
    args = ap.parse_args()
    out_dir, rows = load(args.run_id)
    verdicts = {}
    for line in (out_dir / "verdicts.jsonl").read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            verdicts[(d["case_id"], d["model"], d["sample"],
                      d.get("prompt_variant", "bare"))] = d["verdict"]

    records = []
    for r in rows:
        cid = r["case_id"]
        if cid not in FIXTURES or not r.get("sim_pass"):
            continue
        arm = r.get("prompt_variant", "bare")
        deck = (out_dir / r["netlist_file"]).read_text(errors="ignore")
        res = resolve(deck, BY_ID[cid].part)
        key = (cid, r["model"], r["sample"], arm)
        rec = {"case_id": cid, "model": r["model"], "sample": r["sample"],
               "arm": arm, "netlist_file": r["netlist_file"],
               "grade": verdicts.get(key, "ungraded"),
               "binding": res.binding, "qualified": res.vendor_supplied}
        body, info = take_over(deck)
        if body is None:
            rec.update(status="unsupported", reason=info.get("reason"))
            records.append(rec)
            continue
        fixture = (body.rstrip() + "\n* ---- evaluator-owned fixture ----\n"
                   ".options plotwinsize=0\n" + stimulus(cid, info["offset"]) + ".end\n")
        wave, err = simulate(fixture)
        if wave is None:
            rec.update(status="no_sim", reason=err)
        else:
            rec.update(status="ran", **score(cid, wave))
        records.append(rec)
        print(f"{cid} {arm:<11} {r['model'][:14]:<14} k{r['sample']} "
              f"{rec['grade']:<8} {rec['binding']:<10} {rec['status']:<11} "
              f"{'PASS' if rec.get('all') else '----'}", flush=True)

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)

    def clean(x):
        if isinstance(x, float) and not math.isfinite(x):
            return None
        if isinstance(x, (np.bool_,)):
            return bool(x)
        return x
    (dest / "fixtures.jsonl").write_text("\n".join(
        json.dumps({k: clean(v) for k, v in rec.items()}) for rec in records) + "\n")
    (dest / "preregistration.json").write_text(json.dumps(FIXTURES, indent=1))
    print(f"wrote {len(records)} records -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
