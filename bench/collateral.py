#!/usr/bin/env python3
"""Collateral checks: wrong model variants, opaque exports, and precedence.

    python bench/collateral.py 20260809T211634Z

1. Wrong variant. Each genuine numeric pass in a fixture case that has an
   installed sibling model is rewritten to call the sibling (ADR1399H ->
   ADR1399E, AD8092 -> AD8091, AD8551 -> AD8552), then run through the same
   evaluator-owned fixture as `fixtures.py`. The question is whether a deck
   built on the wrong collateral passes the same functional test, and which
   checks notice.
2. Opaque exports. For every encrypted model a qualified pass binds to,
   LTspice is given a probe deck that loads only that file and instantiates
   the requested name with the candidate's port count. Running without an
   unknown-subcircuit or port-count error is the simulator's own evidence
   that the file exports that name with that arity.
3. Precedence. For each frozen held-out part, a benign stub (each port
   shunted to ground through 1 GOhm) of the part's name is placed before and
   after the library directive; the run is compared with the unmodified
   reference.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

from analyze import load                                   # noqa: E402
from cases import BY_ID                                    # noqa: E402
from detect import instantiates_part, substituted          # noqa: E402
from fixtures import FIXTURES, score, simulate, stimulus, take_over  # noqa: E402
from mutants import (TEMPLATES, _instance_lines, _lib_lines, _nodes,  # noqa: E402
                     _same_response, _simulate, held_out_parts)
from provenance import callee_of, resolve                  # noqa: E402

SIBLINGS = {"ADR1399H": ("ADR1399E", "ADR1399H.sub", "ADR1399E.sub"),
            "AD8092": ("AD8091", None, None),
            "AD8551": ("AD8552", None, None)}


def swap_variant(deck: str, part: str) -> str | None:
    sib, old_file, new_file = SIBLINGS[part]
    out, n = [], 0
    for raw in deck.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line[:1].upper() == "X" and (callee_of(" ".join(line.split())) or "").upper() == part.upper():
            raw = re.sub(rf"(?i)(\s){re.escape(part)}(\s|$)", rf"\g<1>{sib}\g<2>", raw)
            n += 1
        if old_file and re.match(r"(?i)^\s*\.(lib|inc)", line):
            raw = re.sub(re.escape(old_file), new_file, raw, flags=re.I)
        out.append(raw)
    return "\n".join(out) if n else None


def run_fixture(deck: str, case_id: str) -> dict:
    body, info = take_over(deck)
    if body is None:
        return {"status": "unsupported"}
    fx = (body.rstrip() + "\n.options plotwinsize=0\n" + stimulus(case_id, info["offset"]) + ".end\n")
    wave, err = simulate(fx)
    if wave is None:
        return {"status": "no_sim", "reason": err}
    return {"status": "ran", **score(case_id, wave)}


def probe_export(lib_file: str, name: str, nports: int, sha256: str) -> dict:
    """Load only `lib_file`, instantiate `name` with `nports` ports, and
    require a completed run. The probe is tied to the digest of the file the
    resolver bound, and fails if the file on disk no longer has that digest."""
    from app.simulation_runner import find_model
    from provenance import _sha256
    found = find_model(lib_file)
    same_bytes = bool(found) and _sha256(Path(found)) == sha256
    nodes = " ".join(f"p{i}" for i in range(nports))
    loads = "\n".join(f"R{i} p{i} 0 1G" for i in range(nports))
    deck = f"* export probe\n.lib {lib_file}\nX1 {nodes} {name}\n{loads}\n.op\n.end\n"
    sim = _simulate(deck)
    exported = same_bytes and sim["status"] == "ran"
    return {"file": lib_file, "sha256": sha256, "digest_matches": same_bytes,
            "name": name, "ports": nports, "status": sim["status"],
            "exported": exported, "fatal": sim["fatal"]}


def benign_stub(name: str, n: int) -> str:
    pins = " ".join(f"p{i}" for i in range(n))
    return (f".subckt {name} {pins}\n"
            + "\n".join(f"RB{i} p{i} 0 1G" for i in range(n)) + f"\n.ends {name}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--out", default="bench/runs/collateral")
    args = ap.parse_args()
    out_dir, rows = load(args.run_id)
    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    summary: dict = {}

    # 1. wrong variant -------------------------------------------------------
    variants = []
    for r in rows:
        cid = r["case_id"]
        part = BY_ID[cid].part
        if cid not in FIXTURES or part not in SIBLINGS or not r.get("sim_pass"):
            continue
        deck = (out_dir / r["netlist_file"]).read_text(errors="ignore")
        if not resolve(deck, part).vendor_supplied:
            continue
        swapped = swap_variant(deck, part)
        if swapped is None:
            continue
        res = resolve(swapped, part)
        fx = run_fixture(swapped, cid)
        rec = {"case_id": cid, "model": r["model"], "sample": r["sample"],
               "arm": r.get("prompt_variant", "bare"), "from": part,
               "to": SIBLINGS[part][0], "fixture": fx,
               "fixture_pass": bool(fx.get("all")),
               "argus_accepts": res.vendor_supplied, "argus_binding": res.binding,
               "stem_check_accepts": (instantiates_part(swapped, part)
                                      and not substituted(swapped, part))}
        variants.append(rec)
        print(f"variant {cid} {rec['from']}->{rec['to']} fixture={rec['fixture_pass']} "
              f"argus={rec['argus_accepts']} stem={rec['stem_check_accepts']}", flush=True)
    summary["variants"] = {
        "n": len(variants),
        "fixture_pass": sum(v["fixture_pass"] for v in variants),
        "argus_rejects": sum(not v["argus_accepts"] for v in variants),
        "stem_accepts": sum(v["stem_check_accepts"] for v in variants),
        "by_swap": dict(Counter(f"{v['from']}->{v['to']}" for v in variants)),
    }

    # 2. opaque exports ------------------------------------------------------
    probes, seen = [], set()
    qualified_opaque = 0
    for r in rows:
        if not r.get("sim_pass") or r.get("manual_grade_only"):
            continue
        part = BY_ID[r["case_id"]].part
        deck = (out_dir / r["netlist_file"]).read_text(errors="ignore")
        res = resolve(deck, part)
        if not (res.vendor_supplied and res.opaque):
            continue
        qualified_opaque += 1
        f = res.vendor_files[0]
        nports = len((res.vendor_instance or "").split()) - 2
        key = (Path(f.path).name, part.upper(), nports, f.sha256)
        if key in seen:
            continue
        seen.add(key)
        p = probe_export(*key)
        probes.append(p)
        print(f"probe {key} exported={p['exported']} status={p['status']}", flush=True)
    summary["opaque"] = {"qualified_passes": qualified_opaque,
                         "distinct_bindings": len(probes),
                         "exported": sum(p["exported"] for p in probes)}

    # 3. precedence ----------------------------------------------------------
    import random
    prec = []
    for part, path in held_out_parts(40, random.Random(0)):
        ref = path.read_text(encoding="utf-8", errors="ignore")
        insts, libs = _instance_lines(ref, part), _lib_lines(ref, part)
        if not insts or not libs:
            continue
        n = len(_nodes(insts[0])[0])
        stub = benign_stub(part, n)
        base = _simulate(ref)
        before = _simulate(ref.replace(libs[0], stub + libs[0], 1))
        after = _simulate(ref.replace(".end", stub + ".end", 1))
        rec = {"part": part,
               "before_status": before["status"], "after_status": after["status"],
               "before_same": before["status"] == "ran" and _same_response(before["final"], base["final"]),
               "after_same": after["status"] == "ran" and _same_response(after["final"], base["final"])}
        prec.append(rec)
        print(f"precedence {part}: before same={rec['before_same']} ({before['status']}) "
              f"after same={rec['after_same']} ({after['status']})", flush=True)
    summary["precedence"] = {
        "n": len(prec),
        "after_reproduces": sum(p["after_same"] for p in prec),
        "before_changes": sum(p["before_status"] == "ran" and not p["before_same"] for p in prec),
        "before_reproduces": [p["part"] for p in prec if p["before_same"]],
        "before_fails": sum(p["before_status"] != "ran" for p in prec),
        "after_fails": sum(p["after_status"] != "ran" for p in prec),
    }

    (dest / "variants.jsonl").write_text("\n".join(json.dumps(v, default=str) for v in variants) + "\n")
    (dest / "probes.jsonl").write_text("\n".join(json.dumps(p) for p in probes) + "\n")
    (dest / "precedence.jsonl").write_text("\n".join(json.dumps(p) for p in prec) + "\n")
    (dest / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
