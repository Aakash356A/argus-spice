#!/usr/bin/env python3
"""Same-name, wrong-binding experiment: where full resolution beats a
structural baseline on decks that pass the same functional test.

    python bench/samename.py 20260809T211634Z

Every deck here keeps a reachable instance of the exact requested name and
the approved library directive, so a structural check of the kind
"exact-name instance + approved include" accepts it. The simulator is
nevertheless made to bind the name to a different definition, through one of
three mechanisms a real flow can produce:

  transitive  a board-level include, placed before the approved library,
              pulls in a legacy file that defines the same name
  path        a modified copy of the approved library earlier on the library
              search path (LTSPICE_LIB_PATH), which both the simulation runner
              and the resolver honour

A third mechanism, selecting a non-approved `.lib` section, cannot arise here:
LTspice 17.2.4 does not implement library sections (see provenance.py), and
the resolver returns `unsupported` for any sectioned load.

The substituted definition is another op-amp model from the installed
ADI.lib with the same five-port convention, renamed to the requested part.
Bodies identical to the requested model (single/dual counterparts) are
excluded, so every substitute is electrically different. Which substitute is
used is fixed by rule, not chosen: alternatives are swept on one reference
deck per part, and the first one in library order that passes the
functional fixture is used for every deck and construction.

For each deck and construction we record: whether the functional fixture of
`fixtures.py` passes; whether the response differs from the unmodified deck
(the simulator's own evidence that it bound the substituted definition);
and the verdicts of the numeric gate, two structural baselines, and ARGUS.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

import provenance                                         # noqa: E402
from analyze import load                                  # noqa: E402
from cases import BY_ID                                   # noqa: E402
from fixtures import score, simulate, stimulus, take_over  # noqa: E402

CASES = ("B1", "B4")
APPROVED = {"ADI.LIB", "OPAMP_CORNERS.LIB"}
PORTS = ["1", "2", "99", "50", "45"]
_INC = re.compile(r'(?i)^\s*\.(lib|inc|include)\s+("?)([^"\s]+)\2')


def adi_text() -> str:
    from app.simulation_runner import find_model
    return Path(find_model("ADI.lib")).read_text(errors="ignore")


def bodies(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'(?ims)^\.SUBCKT\s+(\S+)\s+([^\n]*)\n.*?^\.ENDS\b[^\n]*', text):
        if m.group(2).split() == PORTS:
            out[m.group(1).upper()] = m.group(0)
    return out


def renamed(body: str, new: str) -> str:
    lines = body.splitlines()
    lines[0] = re.sub(r'(?i)^(\.SUBCKT\s+)\S+', rf'\g<1>{new}', lines[0])
    lines[-1] = re.sub(r'(?i)^(\.ENDS)\b.*', rf'\g<1> {new}', lines[-1])
    return "\n".join(lines) + "\n"


def _norm(body: str, name: str) -> list[str]:
    return [l.replace(name, "X").strip() for l in body.splitlines()
            if l.strip() and not l.strip().startswith("*")][1:-1]


# ------------------------------------------------------------------ baselines
def top_includes(deck: str) -> list[str]:
    names = []
    for line in provenance.logical_lines(deck):
        m = _INC.match(line)
        if m:
            names.append(Path(m.group(3).replace("\\", "/")).name.upper())
    return names


def exact_instance(deck: str, part: str) -> bool:
    return any(c.upper() == part.upper()
               for _, c in provenance.elaborated_instances(deck))


def baseline_name_include(deck: str, part: str) -> bool:
    """Reachable exact-name instance plus an approved include."""
    return exact_instance(deck, part) and any(n in APPROVED for n in top_includes(deck))


def baseline_strict(deck: str, part: str) -> bool:
    """As above, and every top-level include is approved."""
    inc = [n for n in top_includes(deck) if not n.startswith("STANDARD.")]
    return exact_instance(deck, part) and bool(inc) and all(n in APPROVED for n in inc)


# ------------------------------------------------------------- constructions
def construct(kind: str, deck: str, part: str, alt_body: str, genuine: str,
              libdir: Path, adi: str) -> str:
    sub = libdir / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    first_adi = next((l for l in deck.splitlines()
                      if _INC.match(l) and "adi.lib" in l.lower()), None)
    if first_adi is None:
        raise ValueError("deck does not load ADI.lib")
    if kind == "transitive":
        (sub / "legacy_opamps.sub").write_text(renamed(alt_body, part))
        (sub / "board_models.lib").write_text(".inc legacy_opamps.sub\n")
        return deck.replace(first_adi, ".inc board_models.lib\n" + first_adi, 1)
    if kind == "path":
        modified = adi.replace(genuine, renamed(alt_body, part), 1)
        assert modified != adi
        (sub / "ADI.lib").write_text(modified)
        return deck
    raise ValueError(kind)


def run_fixture(deck: str, cid: str) -> dict:
    body, info = take_over(deck)
    if body is None:
        return {"status": "unsupported"}
    fx = body.rstrip() + "\n.options plotwinsize=0\n" + stimulus(cid, info["offset"]) + ".end\n"
    wave, err = simulate(fx)
    if wave is None:
        return {"status": "no_sim", "reason": err}
    return {"status": "ran", **score(cid, wave)}


def differs(a: dict, b: dict) -> bool | None:
    if a.get("status") != "ran" or b.get("status") != "ran":
        return None
    return (abs(a["gain_db"] - b["gain_db"]) > 1e-3
            or abs(a["phase_deg"] - b["phase_deg"]) > 1e-2)


def argus(deck: str, part: str, libdir: Path, snapshot: dict):
    res = provenance.resolve(deck, part, snapshot=snapshot)
    return res.vendor_supplied, res.binding


def in_env(libdir: Path, fn, *a):
    old = os.environ.get("LTSPICE_LIB_PATH")
    os.environ["LTSPICE_LIB_PATH"] = str(libdir)
    try:
        return fn(*a)
    finally:
        if old is None:
            os.environ.pop("LTSPICE_LIB_PATH", None)
        else:
            os.environ["LTSPICE_LIB_PATH"] = old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--out", default="bench/runs/samename")
    args = ap.parse_args()
    out_dir, rows = load(args.run_id)
    adi = adi_text()
    lib = bodies(adi)
    snapshot = dict(provenance.load_snapshot())

    decks = {}
    for r in rows:
        if r["case_id"] in CASES and r.get("sim_pass"):
            text = (out_dir / r["netlist_file"]).read_text(errors="ignore")
            part = BY_ID[r["case_id"]].part
            if provenance.resolve(text, part).vendor_supplied:
                decks.setdefault(r["case_id"], []).append((r, text))

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    sweep, chosen = [], {}
    for cid in CASES:
        part = BY_ID[cid].part
        genuine = lib[part.upper()]
        ref_row, ref_deck = decks[cid][0]
        base = run_fixture(ref_deck, cid)
        for alt in lib:
            if alt == part.upper() or _norm(lib[alt], alt) == _norm(genuine, part.upper()):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                libdir = Path(tmp) / "lib"
                deck = construct("transitive", ref_deck, part, lib[alt], genuine, libdir, adi)
                fx = in_env(libdir, run_fixture, deck, cid)
            rec = {"case_id": cid, "part": part, "alternative": alt,
                   "fixture_pass": bool(fx.get("all")), "differs": differs(fx, base),
                   "status": fx.get("status")}
            sweep.append(rec)
            if rec["fixture_pass"] and rec["differs"] and cid not in chosen:
                chosen[cid] = alt
            print(f"sweep {cid} {alt:<10} pass={rec['fixture_pass']} differs={rec['differs']}",
                  flush=True)

    records = []
    for cid in CASES:
        part = BY_ID[cid].part
        genuine = lib[part.upper()]
        alt = chosen.get(cid)
        if alt is None:
            continue
        for r, text in decks[cid]:
            base = run_fixture(text, cid)
            for kind in ("transitive", "path"):
                with tempfile.TemporaryDirectory() as tmp:
                    libdir = Path(tmp) / "lib"
                    deck = construct(kind, text, part, lib[alt], genuine, libdir, adi)
                    snap = dict(snapshot)
                    control = None
                    fx = in_env(libdir, run_fixture, deck, cid)
                    acc, binding = in_env(libdir, argus, deck, part, libdir, snap)
                rec = {"case_id": cid, "model": r["model"], "sample": r["sample"],
                       "arm": r.get("prompt_variant", "bare"), "construction": kind,
                       "alternative": alt,
                       "fixture_pass": bool(fx.get("all")),
                       "simulator_bound_substitute": differs(fx, base),
                       "baseline_name_include": baseline_name_include(deck, part),
                       "baseline_strict": baseline_strict(deck, part),
                       "argus_accepts": acc, "argus_binding": binding,
                       "control": control}
                records.append(rec)
                print(f"{cid} {kind:<10} k{r['sample']} {r['model'][:10]} "
                      f"fixture={rec['fixture_pass']} bound_alt={rec['simulator_bound_substitute']} "
                      f"base={rec['baseline_name_include']} strict={rec['baseline_strict']} "
                      f"argus={acc}/{binding}", flush=True)

    (dest / "sweep.jsonl").write_text("\n".join(json.dumps(x) for x in sweep) + "\n")
    (dest / "samename.jsonl").write_text("\n".join(json.dumps(x) for x in records) + "\n")
    summary = {"chosen": chosen,
               "sweep": {cid: {"alternatives": sum(1 for x in sweep if x["case_id"] == cid),
                               "pass": sum(1 for x in sweep if x["case_id"] == cid and x["fixture_pass"]),
                               "pass_and_differ": sum(1 for x in sweep if x["case_id"] == cid
                                                      and x["fixture_pass"] and x["differs"])}
                         for cid in CASES}}
    for kind in ("transitive", "path"):
        sub = [x for x in records if x["construction"] == kind]
        summary[kind] = {
            "n": len(sub),
            "fixture_pass": sum(x["fixture_pass"] for x in sub),
            "simulator_bound_substitute": sum(bool(x["simulator_bound_substitute"]) for x in sub),
            "baseline_accepts": sum(x["baseline_name_include"] for x in sub),
            "strict_baseline_accepts": sum(x["baseline_strict"] for x in sub),
            "argus_rejects": sum(not x["argus_accepts"] for x in sub),
            "bindings": sorted({x["argus_binding"] for x in sub}),
        }
    (dest / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
