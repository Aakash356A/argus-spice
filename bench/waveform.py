#!/usr/bin/env python3
"""Waveform-level checks for probe netlists that passed numeric scoring.

Static audits identify fabrication and wiring mistakes, but cannot see whether
a real circuit distorts a transient waveform while still meeting a simple
peak-to-peak gain target. This module re-simulates stored netlists with a raw
file retained long enough to measure output THD and clipping.

Usage:

    python bench/waveform.py <run-id>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.simulation_runner import SimulationRunner  # noqa: E402
from bench.cases import BY_ID  # noqa: E402
from bench.detect import analysis_directives, has_time_varying_stimulus  # noqa: E402
from measure_raw import _get_trace, load_traces  # noqa: E402


RUNS_DIR = Path(__file__).resolve().parent / "runs"
THD_LIMIT = 0.10
CLIP_LIMIT = 0.10
SIMULATION_ATTEMPTS = 2

# SimulationRunner configures this logger at DEBUG level for the GUI. A serial
# audit of roughly one hundred netlists should instead leave the terminal for
# resumable progress and the final aggregate.
logging.getLogger("app.simulation_runner").setLevel(logging.WARNING)


def _skipped(reason: str) -> tuple[bool, dict[str, Any]]:
    return False, {"skipped": reason}


def _raw_after_run(result: dict, net_path: Path, remaining_s: float) -> Path | None:
    """Wait for a raw file when macOS launches LTspice asynchronously.

    `open -W -a LTspice` can return zero before a longer batch simulation has
    written its output. The runner still owns process startup; this only keeps
    its isolated working directory alive until the expected raw file appears.
    """
    raw_path = Path(result.get("raw_path") or net_path.with_suffix(".raw"))
    if result.get("ok") and raw_path.exists():
        return raw_path
    if result.get("returncode") != 0:
        return None
    deadline = time.monotonic() + max(0.0, remaining_s)
    while time.monotonic() < deadline:
        if raw_path.exists():
            return raw_path
        time.sleep(0.1)
    return raw_path if raw_path.exists() else None


def _waveform_metrics(time: np.ndarray, output: np.ndarray,
                      freq_hz: float) -> dict[str, float] | None:
    """Return steady-state THD and clipping metrics, or None for unusable data."""
    start = len(time) // 2
    time = np.asarray(time[start:], dtype=float)
    output = np.asarray(output[start:])
    output = output.real if np.iscomplexobj(output) else output.astype(float)
    finite = np.isfinite(time) & np.isfinite(output)
    time, output = time[finite], output[finite]
    if len(time) < 32:
        return None

    # LTspice transient points are adaptively spaced. A uniform interpolation
    # makes the FFT's frequency axis meaningful without replacing its parser.
    time, unique = np.unique(time, return_index=True)
    output = output[unique]
    if len(time) < 32 or time[-1] <= time[0]:
        return None
    uniform_time = np.linspace(time[0], time[-1], len(time))
    uniform_output = np.interp(uniform_time, time, output)
    sample_interval = float(uniform_time[1] - uniform_time[0])
    if sample_interval <= 0:
        return None

    minimum, maximum = float(np.min(uniform_output)), float(np.max(uniform_output))
    span = maximum - minimum
    if span <= np.finfo(float).eps:
        return None

    # A Hann window limits leakage when a .tran stop time ends mid-cycle. Zero
    # padding improves the nearest-bin estimate without inventing new samples.
    sample_count = len(uniform_output)
    fft_count = 1 << (sample_count - 1).bit_length()
    fft_count *= 8
    spectrum = np.abs(np.fft.rfft(
        (uniform_output - np.mean(uniform_output)) * np.hanning(sample_count),
        n=fft_count,
    ))
    frequencies = np.fft.rfftfreq(fft_count, d=sample_interval)
    fundamental_index = int(np.argmin(np.abs(frequencies - freq_hz)))
    fundamental = float(spectrum[fundamental_index])
    if fundamental <= np.finfo(float).eps:
        return None

    harmonic_indices = set()
    for harmonic in range(2, 11):
        target = harmonic * freq_hz
        if target > frequencies[-1]:
            continue
        index = int(np.argmin(np.abs(frequencies - target)))
        if index != fundamental_index:
            harmonic_indices.add(index)
    harmonic_power = sum(float(spectrum[index]) ** 2 for index in harmonic_indices)
    thd = float(np.sqrt(harmonic_power) / fundamental)

    edge = 0.01 * span
    low_clip = float(np.mean(uniform_output <= minimum + edge))
    high_clip = float(np.mean(uniform_output >= maximum - edge))
    # Assess the rails separately. Counting both turning points together would
    # mark a clean sine as clipped even though it has no flat top at either rail.
    clipped = max(low_clip, high_clip)
    return {
        "thd": thd,
        "fundamental_hz": float(frequencies[fundamental_index]),
        "clipped": clipped,
        "clipped_low": low_clip,
        "clipped_high": high_clip,
    }


def linearity_violation(netlist: str, in_node: str, out_node: str,
                        freq_hz: float, timeout_s: int = 60) -> tuple[bool, dict]:
    """Re-simulate a transient waveform and flag excessive THD or clipping.

    The caller owns the case-level ``gain_db`` applicability check because the
    case metrics are not part of this deliberately small public API.
    """
    if not has_time_varying_stimulus(netlist):
        return _skipped("no time-varying stimulus")
    if ".tran" not in analysis_directives(netlist):
        return _skipped("no transient analysis")

    try:
        runner = SimulationRunner()
        if not runner.is_available():
            return _skipped("did not converge")
        for attempt in range(SIMULATION_ATTEMPTS):
            # SimulationRunner rewrites resolvable .lib paths, so use an
            # isolated copy rather than modifying the stored candidate while
            # retaining raw. A fresh launch also absorbs transient macOS app
            # startup failures observed only in long serial batches.
            with tempfile.TemporaryDirectory(prefix="spice-waveform-") as directory:
                net_path = Path(directory) / "candidate.net"
                net_path.write_text(netlist, encoding="utf-8")
                started = time.monotonic()
                result = runner.run_batch(net_path, timeout=timeout_s)
                raw_path = _raw_after_run(
                    result, net_path, timeout_s - (time.monotonic() - started))
                if raw_path is None:
                    continue
                try:
                    traces = load_traces(raw_path)
                    time_axis = np.asarray(traces.get_time(case=0))
                    output = _get_trace(traces, out_node, case=0)
                except Exception:  # noqa: BLE001 - malformed raw must only skip.
                    return _skipped("output trace unavailable")
                break
        else:
            return _skipped("did not converge")
    except Exception:  # noqa: BLE001 - an audit must never stop a run.
        return _skipped("did not converge")

    metrics = _waveform_metrics(time_axis, output, freq_hz)
    if metrics is None:
        return _skipped("insufficient waveform data")
    violation = metrics["thd"] > THD_LIMIT or metrics["clipped"] > CLIP_LIMIT
    return violation, metrics


def _identity(record: dict) -> tuple[str, str, int | str, str]:
    return (
        str(record.get("case_id", "")),
        str(record.get("model", "")),
        record.get("sample", ""),
        str(record.get("prompt_variant", "bare")),
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _linearity_record(out_dir: Path, row: dict, timeout_s: int) -> dict:
    case_id = row.get("case_id", "")
    record = {
        "case_id": case_id,
        "model": row.get("model", ""),
        "sample": row.get("sample", ""),
        "prompt_variant": row.get("prompt_variant", "bare"),
        "violation": False,
        "thd": None,
        "clipped": None,
        "skipped": None,
    }
    case = BY_ID.get(case_id)
    if case is None:
        record["skipped"] = "unknown case"
        return record
    if "gain_db" not in {name for name, _, _ in case.metrics}:
        record["skipped"] = "no gain_db metric"
        return record
    netlist_file = row.get("netlist_file")
    if not netlist_file:
        record["skipped"] = "netlist missing"
        return record
    netlist_path = out_dir / netlist_file
    if not netlist_path.exists():
        record["skipped"] = "netlist missing"
        return record

    netlist = netlist_path.read_text(encoding="utf-8", errors="ignore")
    violation, details = linearity_violation(
        netlist, case.in_node, case.out_node, case.freq_hz, timeout_s)
    record["violation"] = violation
    record["thd"] = details.get("thd")
    record["clipped"] = details.get("clipped")
    record["skipped"] = details.get("skipped")
    if "fundamental_hz" in details:
        record["fundamental_hz"] = details["fundamental_hz"]
    return record


def _print_summary(records: list[dict]) -> None:
    def counts(group: list[dict]) -> tuple[int, int, int]:
        skipped = sum(bool(record.get("skipped")) for record in group)
        checked = len(group) - skipped
        violated = sum(bool(record.get("violation")) for record in group)
        return checked, skipped, violated

    checked, skipped, violated = counts(records)
    print(f"linearity: checked {checked}, skipped {skipped}, violated {violated}")
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        by_key[(record["model"], record.get("prompt_variant", "bare"))].append(record)
    for (model, variant), group in sorted(by_key.items()):
        checked, skipped, violated = counts(group)
        print(f"  {model:<34} {variant:<8} checked {checked:>3} "
              f"skipped {skipped:>3} violated {violated:>3}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Probe run id below bench/runs/")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Per-simulation timeout in seconds (default: 60).")
    args = parser.parse_args()

    out_dir = RUNS_DIR / args.run_id
    results_path = out_dir / "results.jsonl"
    if not results_path.exists():
        sys.exit(f"No results at {results_path}")
    output_path = out_dir / "linearity.jsonl"
    source_rows = [row for row in _read_jsonl(results_path) if row.get("sim_pass") is True]
    completed = _read_jsonl(output_path)
    seen = {_identity(record) for record in completed}

    with output_path.open("a", encoding="utf-8") as output:
        for row in source_rows:
            if _identity(row) in seen:
                continue
            record = _linearity_record(out_dir, row, args.timeout)
            output.write(json.dumps(record, allow_nan=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
            completed.append(record)
            seen.add(_identity(record))

    _print_summary(completed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())