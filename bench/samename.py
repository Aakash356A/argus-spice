#!/usr/bin/env python3
"""Same name, different model: decks that pass the functional test, keep the
exact requested name, and still run another definition.

    python bench/samename.py 20260809T211634Z

Each genuine B1/B4 pass is rewritten through three mechanisms. Every rewrite
has a matched control that is staged the same way but leaves the requested
model in place, so a check is scored on what it rejects and on what it keeps.

  transitive  a board-level include placed before the approved library pulls
              in a legacy file. Altered: the file defines the requested name.
              Control: it defines the same body under another name.
  path        a copy of the approved library sits earlier on the library search
              path. Altered: the requested model's body is replaced.
              Control: the copy is byte-identical.
  nested      the approved library is staged as a release whose requested model
              lives in a nested helper file. Altered: only the helper changes,
              so the deck and every directly included file are byte-identical
              to the control's. The staged release (library + clean helper) is
              what the snapshot pins. The installed ADI.lib is monolithic, so
              this layout is constructed for the experiment.

The substituted definition is another op-amp model from the installed ADI.lib
with the same five-port convention, renamed to the requested part; bodies
identical to the requested model are excluded. Which one is used is fixed by
rule: alternatives are swept on one reference deck per part, and the first in
library order that passes the fixture with a different response is used.

Three simulator runs per rewrite identify the model by measurement: the matched
control, the altered deck, and a direct reference in which the unmodified
library is loaded and the instance calls the alternative by its own name. The
substitute is taken to have run when the altered response equals the direct
reference and differs from the control. Resolved digests are stored for both.
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
from baselines import BASELINES, verdicts                 # noqa: E402
from cases import BY_ID                                   # noqa: E402
from fixtures import score, simulate, stimulus, take_over  # noqa: E402

CASES = ("B1", "B4")
KINDS = ("transitive", "path", "nested")
PORTS = ["1", "2", "99", "50", "45"]
_INC = re.compile(r'(?i)^\s*\.(lib|inc|include)\s+("?)([^"\s]+)\2')
GAIN_TOL, PHASE_TOL = 1e-3, 1e-2        # dB, degrees: "different response"


def adi_text() -> str:
    """ADI.lib as latin-1, which maps every byte, so writing it back is exact."""
    from app.simulation_runner import find_model
    return Path(find_model("ADI.lib")).read_bytes().decode("latin-1")


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


def call_instead(deck: str, part: str, other: str) -> str:
    """The deck with every call of `part` renamed to `other`."""
    out = []
    for raw in deck.splitlines():
        line = " ".join(raw.split(";", 1)[0].split())
        if line[:1].upper() == "X" and (provenance.callee_of(line) or "").upper() == part.upper():
            raw = re.sub(rf"(?i)(\s){re.escape(part)}(\s|$)", rf"\g<1>{other}\g<2>", raw)
        out.append(raw)
    return "\n".join(out)


def construct(kind: str, altered: bool, deck: str, part: str, alt_body: str,
              genuine: str, libdir: Path, adi: str, snapshot: dict):
    """Write the staged library and return (deck, snapshot to judge it by)."""
    sub = libdir / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    first_adi = next((l for l in deck.splitlines()
                      if _INC.match(l) and "adi.lib" in l.lower()), None)
    if first_adi is None:
        raise ValueError("deck does not load ADI.lib")
    if kind == "transitive":
        name = part if altered else part + "_BOARDSPARE"
        (sub / "legacy_opamps.sub").write_text(renamed(alt_body, name), encoding="latin-1")
        (sub / "board_models.lib").write_text(".inc legacy_opamps.sub\n")
        return deck.replace(first_adi, ".inc board_models.lib\n" + first_adi, 1), snapshot
    if kind == "path":
        text = adi.replace(genuine, renamed(alt_body, part), 1) if altered else adi
        assert (text != adi) == altered
        (sub / "ADI.lib").write_bytes(text.encode("latin-1"))
        return deck, snapshot
    if kind == "nested":
        helper = f"{part}_core.sub"
        library = adi.replace(genuine, f".inc {helper}", 1)
        assert library != adi
        (sub / "ADI.lib").write_bytes(library.encode("latin-1"))
        clean = renamed(genuine, part)
        (sub / helper).write_text(clean, encoding="latin-1")
        staged = dict(snapshot)
        for f in ("ADI.lib", helper):
            path = (sub / f).resolve()
            staged[provenance._normalise_path(str(path))] = provenance._sha256(path)
        if altered:
            (sub / helper).write_text(renamed(alt_body, part), encoding="latin-1")
        return deck, staged
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
    return (abs(a["gain_db"] - b["gain_db"]) > GAIN_TOL
            or abs(a["phase_deg"] - b["phase_deg"]) > PHASE_TOL)


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


def judge(deck: str, part: str, snapshot: dict) -> dict:
    res = provenance.resolve(deck, part, snapshot=snapshot)
    ev = res.evidence()
    return {**verdicts(deck, part, snapshot), "argus": res.vendor_supplied,
            "binding": res.binding,
            "direct_files": [[f["resolved"], f["sha256"], f["pinned"]]
                             for f in ev["transitive_includes"] if f["depth"] == 1],
            "nested_files": [[f["resolved"], f["sha256"], f["pinned"]]
                             for f in ev["transitive_includes"] if f["depth"] > 1]}


def stage(kind, altered, text, cid, part, alt_body, genuine, adi, snapshot):
    """Fixture result and every check's verdict for one staged deck."""
    with tempfile.TemporaryDirectory() as tmp:
        libdir = Path(tmp) / "lib"
        deck, snap = construct(kind, altered, text, part, alt_body, genuine,
                               libdir, adi, snapshot)
        return (in_env(libdir, run_fixture, deck, cid),
                in_env(libdir, judge, deck, part, snap))


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
        _, ref_deck = decks[cid][0]
        base = run_fixture(ref_deck, cid)
        for alt in lib:
            if alt == part.upper() or _norm(lib[alt], alt) == _norm(genuine, part.upper()):
                continue
            fx, _ = stage("transitive", True, ref_deck, cid, part, lib[alt],
                          genuine, adi, snapshot)
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
            original = judge(text, part, snapshot)
            reference = run_fixture(call_instead(text, part, alt), cid)
            for kind in KINDS:
                cfx, cj = stage(kind, False, text, cid, part, lib[alt], genuine, adi, snapshot)
                afx, aj = stage(kind, True, text, cid, part, lib[alt], genuine, adi, snapshot)
                matches = differs(afx, reference)
                rec = {"case_id": cid, "model": r["model"], "sample": r["sample"],
                       "arm": r.get("prompt_variant", "bare"), "construction": kind,
                       "alternative": alt,
                       "original": {"verdicts": original},
                       "control": {"fixture": cfx, "verdicts": cj},
                       "altered": {"fixture": afx, "verdicts": aj},
                       "direct_reference": reference,
                       "altered_differs_from_control": differs(afx, cfx),
                       "altered_matches_reference": None if matches is None else not matches,
                       "direct_files_identical": cj["direct_files"] == aj["direct_files"]}
                records.append(rec)
                show = lambda j: "".join("A" if j[b] else "r" for b in (*BASELINES, "argus"))
                print(f"{cid} {kind:<10} k{r['sample']} {r['model'][:10]} "
                      f"ctl={cfx.get('all')} alt={afx.get('all')} "
                      f"differs={rec['altered_differs_from_control']} "
                      f"=ref={rec['altered_matches_reference']} "
                      f"control {show(cj)} altered {show(aj)}/{aj['binding']}", flush=True)

    (dest / "sweep.jsonl").write_text("\n".join(json.dumps(x) for x in sweep) + "\n")
    (dest / "samename.jsonl").write_text("\n".join(json.dumps(x, default=bool) for x in records) + "\n")
    summary = {"chosen": chosen,
               "sweep": {cid: {"alternatives": sum(1 for x in sweep if x["case_id"] == cid),
                               "pass": sum(1 for x in sweep if x["case_id"] == cid and x["fixture_pass"]),
                               "pass_and_differ": sum(1 for x in sweep if x["case_id"] == cid
                                                      and x["fixture_pass"] and x["differs"])}
                         for cid in CASES}}
    for kind in KINDS:
        sub = [x for x in records if x["construction"] == kind]
        summary[kind] = {
            "n": len(sub),
            "control_fixture_pass": sum(bool(x["control"]["fixture"].get("all")) for x in sub),
            "fixture_pass": sum(bool(x["altered"]["fixture"].get("all")) for x in sub),
            "differs_from_control": sum(bool(x["altered_differs_from_control"]) for x in sub),
            "matches_reference": sum(bool(x["altered_matches_reference"]) for x in sub),
            "direct_files_identical": sum(x["direct_files_identical"] for x in sub),
            "control_accepts": {b: sum(x["control"]["verdicts"][b] for x in sub)
                                for b in (*BASELINES, "argus")},
            "altered_accepts": {b: sum(x["altered"]["verdicts"][b] for x in sub)
                                for b in (*BASELINES, "argus")},
            "bindings": sorted({x["altered"]["verdicts"]["binding"] for x in sub}),
        }
    uniq = {(x["case_id"], x["model"], x["sample"], x["arm"]): x["original"]["verdicts"]
            for x in records}
    summary["original_accepts"] = {"n": len(uniq), **{
        b: sum(v[b] for v in uniq.values()) for b in (*BASELINES, "argus")}}
    (dest / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
