"""Held-out validation: known-label provenance mutations on unseen parts.

The audit's predicates were written against the 20-case probe suite, so
reporting their precision and recall on that same suite measures fit, not
generalisation. This module builds a held-out constructed test set with labels that
come from construction rather than from judgement.

Sources are vendor reference netlists in `data/templates/` for parts that
appear in *no* probe case, so nothing here was available while the detectors
were being written. Each reference is a genuine, vendor-supplied design, and
each mutation applies one provenance or interface defect whose label follows
from the edit itself:

    identity        unchanged                                  valid
    wrapper         part wrapped in a local pass-through       valid
    substitute      `.lib` replaced by a local stub            fake
    renamed_stub    local stub under a variant part name       fake
    shadow          genuine `.lib` kept *and* a local stub     fake
    drop_library    instance kept, `.lib` deleted              fake
    remove_part     instance deleted, name left in a comment   fake
    alias_file      `.lib` retargeted at an unrelated file     fake
    pin_permute     two instance pins transposed               fake

`wrapper` and `identity` are the ones that matter most: a check that flags
everything scores perfect recall, so the test set has to contain cases that
must *not* be flagged, including one (`wrapper`) specifically constructed to
look like a substitution while preserving provenance.

No API calls; every mutation is a text edit over files
already on disk.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cases import ALL_CASES                                    # noqa: E402
from detect import (_part_stem, _same_part, _significant_lines,  # noqa: E402
                    instantiates_part, substituted)
from baselines import BASELINES, verdicts as baseline_verdicts  # noqa: E402
from provenance import callee_of, load_snapshot, resolve       # noqa: E402

try:
    from app.simulation_runner import find_model                # noqa: E402
except Exception:                                               # noqa: BLE001
    def find_model(fullname):                                   # type: ignore[misc]
        return None

FROZEN_PARTS = (Path(__file__).resolve().parent.parent / "data"
                / "heldout_parts.json")
SNAPSHOT = (Path(__file__).resolve().parent.parent / "data"
            / "library_snapshot.json")
# Internal node every constructed stub carries. If a simulated circuit's raw
# file contains a trace through it, the local stub -- not the vendor model --
# was elaborated, which LTspice itself reports independently of our parser.
STUB_MARK = "ARGUS_STUB"

TEMPLATES = Path(__file__).resolve().parent.parent / "data" / "templates"
SEED = 20260820

FAKE_MUTATIONS = ("substitute", "renamed_stub", "shadow", "drop_library",
                  "remove_part", "alias_file", "pin_permute")
VALID_MUTATIONS = ("identity", "wrapper")
# pin_permute leaves provenance intact (it is an interface defect), so the
# provenance comparison below is scored over the other six invalid classes.
INTERFACE_MUTATIONS = ("pin_permute",)


def _instance_lines(netlist: str, part: str) -> list[str]:
    """Physical X lines whose callee is exactly the part (case-insensitive)."""
    out = []
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("*") or line[:1].upper() != "X":
            continue
        callee = callee_of(" ".join(line.split()))
        if callee and callee.upper() == part.upper():
            out.append(raw)
    return out


def _nodes(inst: str) -> tuple[list[str], int]:
    """(pin nets, index of the callee token) of a one-line X instance."""
    tokens = inst.split(";", 1)[0].split()
    callee = callee_of(" ".join(tokens))
    idx = max(i for i, t in enumerate(tokens) if t == callee)
    return tokens[1:idx], idx


def _recall(inst: str, name: str) -> str:
    """The same instance calling `name` instead, parameters preserved."""
    tokens = inst.split(";", 1)[0].split()
    _, idx = _nodes(inst)
    return " ".join(tokens[:idx] + [name] + tokens[idx + 1:])


def _lib_lines(netlist: str, part: str) -> list[str]:
    out = []
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line.lower().startswith((".lib", ".include", ".inc")) \
           and "standard." not in line.lower():
            out.append(raw)
    return out


def _stub(part: str, npins: int) -> str:
    """A behavioural stub of the shape the corpus actually contains: a name,
    the instance's own pin count (so shadowing is never confounded with an
    arity error), and controlled sources rather than device models."""
    n = max(npins, 2)
    pins = " ".join(str(i) for i in range(1, n + 1))
    drive = f"V(2)-V(3)" if n >= 3 else "V(1)"
    return (f".subckt {part} {pins}\n"
            f"E1 {n} 0 VALUE={{{drive}}}\n"
            f"R1 {n} 0 1meg\n"
            f"E2 {STUB_MARK} 0 VALUE={{V(1)}}\n"
            f"R2 {STUB_MARK} 0 1meg\n"
            f".ends {part}\n")


def _decoys() -> list[str]:
    """Installed vendor model files, from the pinned snapshot."""
    try:
        files = json.loads(SNAPSHOT.read_text())["files"]
    except (OSError, ValueError, KeyError):
        return []
    return sorted(Path(k).name for k in files
                  if k.startswith("<LTspice>/lib/sub/") and k.endswith(".sub"))


def _declared_names(path: Path) -> set[str] | None:
    """Subcircuit names a library file declares, or None if it is encrypted."""
    blob = path.read_bytes()
    if b"<Binary File>" in blob[:256]:
        return None
    return {l.split()[1].upper() for l in blob.decode("utf-8", "ignore").splitlines()
            if l.strip().lower().startswith(".subckt") and len(l.split()) >= 2}


def mutate(netlist: str, part: str, kind: str, rng: random.Random) -> str | None:
    """Apply one mutation, or return None when it does not apply to this file."""
    instances = _instance_lines(netlist, part)
    libs = _lib_lines(netlist, part)

    if kind == "identity":
        return netlist

    if kind == "wrapper":
        # Provenance is preserved: the genuine part is still instantiated,
        # just one level down inside a locally defined pass-through.
        if not instances:
            return None
        inst = instances[0]
        nodes, idx = _nodes(inst)
        params = inst.split(";", 1)[0].split()[idx + 1:]
        wrapper_name = f"{_part_stem(part)}_WRAP"
        inner = " ".join(f"n{i}" for i in range(len(nodes)))
        body = (f".subckt {wrapper_name} {inner}\n"
                f"XINNER {inner} {' '.join([part] + params)}\n"
                f".ends {wrapper_name}\n")
        replaced = netlist.replace(inst, " ".join(
            [inst.split()[0]] + nodes + [wrapper_name]))
        return replaced.replace(".end", body + ".end", 1)

    if kind == "substitute":
        if not libs:
            return None
        out = netlist
        for lib in libs:
            out = out.replace(lib, "* library directive removed by mutation")
        npins = len(_nodes(instances[0])[0]) if instances else 8
        return out.replace(".end", _stub(part, npins) + ".end", 1)

    if kind == "renamed_stub":
        # Same substitution, but the local definition and the instance both
        # carry a variant name. Exact-string part matching misses this.
        if not instances or not libs:
            return None
        variant = f"{_part_stem(part)}_3V3"
        out = netlist
        for lib in libs:
            out = out.replace(lib, "* library directive removed by mutation")
        for inst in instances:
            out = out.replace(inst, _recall(inst, variant))
        npins = len(_nodes(instances[0])[0])
        return out.replace(".end", _stub(variant, npins) + ".end", 1)

    if kind == "shadow":
        # The directive that a naive check looks for is still present; a local
        # definition of the same name shadows it. LTspice 17.2.4 binds a name
        # defined both locally and in a `.lib` to whichever definition comes
        # first in the file (see `precedence` in main), so the stub is placed
        # ahead of the directive: the simulated circuit then really runs the
        # stub, and the label does not depend on a precedence assumption.
        if not libs or not instances:
            return None
        npins = len(_nodes(instances[0])[0])
        return netlist.replace(libs[0], _stub(part, npins) + libs[0], 1)

    if kind == "shadow_after":
        # Not a labelled class: the same stub placed *after* the directive,
        # used only to measure the simulator's precedence rule.
        if not libs or not instances:
            return None
        npins = len(_nodes(instances[0])[0])
        return netlist.replace(".end", _stub(part, npins) + ".end", 1)

    if kind == "drop_library":
        if not libs or not instances:
            return None
        out = netlist
        for lib in libs:
            out = out.replace(lib, "* library directive removed by mutation")
        return out

    if kind == "remove_part":
        if not instances:
            return None
        out = netlist
        for inst in instances:
            out = out.replace(inst, f"* {part} omitted by mutation")
        return out

    if kind == "alias_file":
        # The directive resolves to an installed vendor file that declares a
        # different part. The target is checked to exist and, where readable,
        # to declare no subcircuit of the requested name, so this is a
        # wrong-definition test rather than a missing-file test.
        if not libs:
            return None
        others = [d for d in _decoys() if not _same_part(Path(d).stem, part)]
        rng.shuffle(others)
        decoy = None
        for cand in others:
            found = find_model(cand)
            if not found:
                continue
            names = _declared_names(Path(found))
            if names is None or part.upper() not in names:
                decoy = cand
                break
        if decoy is None:
            return None
        out = netlist
        for lib in libs:
            head = lib.split()[0]
            out = out.replace(lib, f"{head} {decoy}")
        return out

    if kind == "pin_permute":
        if not instances:
            return None
        # Transpose the first pair of pins that carry *different* nets, so
        # the edit always changes connectivity. A swap of two pins on the same
        # net is a no-op and is never generated.
        inst = instances[0]
        tokens = inst.split(";", 1)[0].split()
        nodes, idx = _nodes(inst)
        pairs = [(0, len(nodes) - 1)] + [(a, b) for a in range(len(nodes))
                                         for b in range(a + 1, len(nodes))]
        for i, j in pairs:
            if i < j < len(nodes) and nodes[i].upper() != nodes[j].upper():
                nodes[i], nodes[j] = nodes[j], nodes[i]
                return netlist.replace(inst, " ".join(
                    [tokens[0]] + nodes + tokens[idx:]))
        return None

    raise ValueError(f"unknown mutation {kind}")


def _simulate(netlist: str, timeout_s: int = 60) -> dict:
    """LTspice's own verdict on a netlist, independent of our parser.

    `status` is `ran`, `missing_model` (LTspice reports an unknown subcircuit
    or an unopenable library), `timeout`, or `error`. `stub_elaborated` is
    True when the raw file carries a trace through a constructed stub's
    marker node, i.e. the simulator elaborated the local stub.
    """
    import logging
    import tempfile
    logging.disable(logging.CRITICAL)
    from app.simulation_runner import SimulationRunner
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mutant.net"
        path.write_text(netlist)
        result = SimulationRunner().run_batch(path, timeout=timeout_s)
        log = ""
        if result.get("log_path"):
            log = Path(result["log_path"]).read_text(
                encoding="utf-8", errors="ignore").replace("\x00", "")
        low = log.lower()
        fatal = next((l.strip() for l in log.splitlines()
                      if "fatal" in l.lower() or "error" in l.lower()), "")
        traces: list[str] = []
        final: dict[str, float] = {}
        if result.get("raw_path"):
            try:
                import measure_raw
                obj = measure_raw.load_traces(result["raw_path"])
                traces = list(obj.variables)
                for name in traces:
                    if name.lower().startswith("v("):
                        data = obj.get_data(name)
                        if data is not None and len(data):
                            final[name.lower()] = float(abs(data[-1]))
            except Exception:                                    # noqa: BLE001
                traces = []
    if "timeout" in str(result.get("error", "")).lower():
        status = "timeout"
    elif "unknown subcircuit" in low or "could not open" in low:
        status = "missing_model"
    elif result.get("ok"):
        status = "ran"
    else:
        status = "error"
    return {"status": status, "fatal": fatal[:160],
            "stub_elaborated": any(STUB_MARK.lower() in t.lower() for t in traces),
            "final": final}


def _same_response(a: dict, b: dict, rel: float = 1e-3) -> bool:
    """Do two runs end in the same top-level node voltages?"""
    keys = set(a) & set(b)
    if not keys:
        return False
    return all(abs(a[k] - b[k]) <= rel * max(1.0, abs(a[k]), abs(b[k]))
               for k in keys)


def held_out_parts(limit: int, rng: random.Random,
                   refreeze: bool = False) -> list[tuple[str, Path]]:
    """The frozen held-out part list, built once and then read back.

    Selection does not consult the resolver under test. A part qualifies when
    (i) its stem matches no probe-case part, (ii) its reference netlist calls
    a subcircuit of exactly the part's name on one line, (iii) a library
    directive in it names a file that exists in the installed library, and
    (iv) LTspice itself simulates the unmodified reference without reporting
    an unknown subcircuit. The list is written to `data/heldout_parts.json`
    so later runs score the same parts.
    """
    if FROZEN_PARTS.exists() and not refreeze:
        names = json.loads(FROZEN_PARTS.read_text())["parts"]
        return [(n, TEMPLATES / f"{n}.net") for n in names][:limit]

    seen_stems = {_part_stem(c.part) for c in ALL_CASES}
    candidates = []
    for path in sorted(TEMPLATES.glob("*.net")):
        stem = _part_stem(path.stem)
        if not stem or stem in seen_stems:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not _instance_lines(text, path.stem):
            continue
        libs = _lib_lines(text, path.stem)
        if not libs or not all(find_model(l.split()[1].strip('"')) for l in libs):
            continue
        candidates.append((path.stem, path))
        seen_stems.add(stem)
    rng.shuffle(candidates)
    chosen = []
    for part, path in candidates:
        if len(chosen) >= limit:
            break
        sim = _simulate(path.read_text(encoding="utf-8", errors="ignore"))
        if sim["status"] == "ran":
            chosen.append((part, path))
    FROZEN_PARTS.write_text(json.dumps(
        {"selection": "see bench/mutants.py:held_out_parts", "seed": SEED,
         "parts": [p for p, _ in chosen]}, indent=1) + "\n")
    return chosen


def audit_flags(netlist: str, part: str, reference: str) -> dict:
    """The two gating predicates, re-derived exactly as the paper reports them."""
    from detect import pin_role_mismatch
    res = resolve(netlist, part)
    return {
        "provenance_fail": not res.vendor_supplied,
        "pin_role_fail": pin_role_mismatch(netlist, part, reference),
        "binding": res.binding,
        # The naive pair a reader might assume equivalent: the part is never
        # instantiated, or a same-name .subckt is defined in the file.
        "naive_fail": (not instantiates_part(netlist, part)
                       or substituted(netlist, part)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parts", type=int, default=40,
                    help="held-out parts to draw (default 40)")
    ap.add_argument("--out", default="bench/runs/mutants")
    ap.add_argument("--tex", default="generated/mutants.tex",
                    help="where to write the LaTeX macro block")
    ap.add_argument("--simulate", action="store_true",
                    help="also run every mutant through LTspice as an "
                         "independent oracle (a few minutes)")
    ap.add_argument("--refreeze", action="store_true",
                    help="re-select the held-out part list")
    args = ap.parse_args()

    rng = random.Random(SEED)
    parts = held_out_parts(args.parts, rng, refreeze=args.refreeze)
    if not parts:
        print("no held-out parts with a resolvable vendor model", file=sys.stderr)
        return 1

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    records = []
    for part, path in parts:
        reference = path.read_text(encoding="utf-8", errors="ignore")
        for kind in VALID_MUTATIONS + FAKE_MUTATIONS:
            mutated = mutate(reference, part, kind, rng)
            if mutated is None:
                continue
            label = "valid" if kind in VALID_MUTATIONS else "invalid"
            flags = audit_flags(mutated, part, reference)
            rec = {
                "part": part, "reference": path.name, "mutation": kind,
                "label": label, **flags,
                "flagged": bool(flags["provenance_fail"] or flags["pin_role_fail"]),
                "flagged_provenance_only": bool(flags["provenance_fail"]),
            }
            base_v = baseline_verdicts(mutated, part, load_snapshot())
            rec.update({f"baseline_{b}": base_v[b] for b in BASELINES})
            if args.simulate:
                rec["sim"] = _simulate(mutated)
            records.append(rec)

    (dest / "mutants.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")

    def matrix(key):
        tp = sum(1 for r in records if r[key] and r["label"] == "invalid")
        fp = sum(1 for r in records if r[key] and r["label"] == "valid")
        fn = sum(1 for r in records if not r[key] and r["label"] == "invalid")
        tn = sum(1 for r in records if not r[key] and r["label"] == "valid")
        return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "precision": 100.0 * tp / (tp + fp) if tp + fp else None,
                "recall": 100.0 * tp / (tp + fn) if tp + fn else None}

    summary = {
        "parts": len(parts),
        "part_list": [p for p, _ in parts],
        "records": len(records),
        "by_mutation": {},
        "full": matrix("flagged"),
        "provenance_only": matrix("flagged_provenance_only"),
    }
    for kind in VALID_MUTATIONS + FAKE_MUTATIONS:
        sub = [r for r in records if r["mutation"] == kind]
        if not sub:
            continue
        caught = sum(1 for r in sub if r["flagged"])
        summary["by_mutation"][kind] = {
            "n": len(sub), "label": sub[0]["label"], "flagged": caught,
            "rate": 100.0 * caught / len(sub),
        }
    # Resolved binding against the naive pair, on the same population: the
    # provenance-destroying classes and the valid ones.
    destroy = [r for r in records if r["label"] == "invalid"
               and r["mutation"] not in INTERFACE_MUTATIONS]
    valid = [r for r in records if r["label"] == "valid"]
    summary["provenance_vs_naive"] = {
        "destroying_n": len(destroy), "valid_n": len(valid),
        "prov_caught": sum(r["provenance_fail"] for r in destroy),
        "prov_valid_flagged": sum(r["provenance_fail"] for r in valid),
        "naive_caught": sum(r["naive_fail"] for r in destroy),
        "naive_valid_flagged": sum(r["naive_fail"] for r in valid),
        "naive_missed_by_mutation": {
            k: sum(1 for r in destroy if r["mutation"] == k and not r["naive_fail"])
            for k in FAKE_MUTATIONS if k not in INTERFACE_MUTATIONS},
    }
    summary["baselines"] = {
        b: {"invalid_accepted": sum(r[f"baseline_{b}"] for r in destroy),
            "invalid_accepted_by_mutation": {
                k: n for k in FAKE_MUTATIONS if k not in INTERFACE_MUTATIONS
                if (n := sum(r[f"baseline_{b}"] for r in destroy if r["mutation"] == k))},
            "valid_accepted": sum(r[f"baseline_{b}"] for r in valid)}
        for b in BASELINES}
    if args.simulate:
        # What LTspice itself reports, per mutation class. A run that does not
        # complete is counted as that and nothing more: it shows neither which
        # definition was used nor that the label is right. Identity is not an
        # oracle row, because parts were selected by that run succeeding.
        ident = {r["part"]: r["sim"] for r in records if r["mutation"] == "identity"}

        def outcome(r):
            sim = r["sim"]
            if sim["status"] == "missing_model":
                return "missing_model"
            if sim["status"] != "ran":
                return "did_not_run"
            same = _same_response(sim["final"],
                                  ident.get(r["part"], {}).get("final", {}))
            return "same" if same else "changed"

        oracle = {}
        for kind in VALID_MUTATIONS + FAKE_MUTATIONS:
            sub = [r for r in records if r["mutation"] == kind]
            if not sub:
                continue
            counts = {}
            for r in sub:
                counts[outcome(r)] = counts.get(outcome(r), 0) + 1
            oracle[kind] = {"n": len(sub), "outcome": counts}
        summary["ltspice_oracle"] = oracle
        # Precedence probe: the shadow stub placed after the directive instead
        # of before it. Does the simulator still reproduce the reference?
        after = []
        for part, path in parts:
            ref = path.read_text(encoding="utf-8", errors="ignore")
            m = mutate(ref, part, "shadow_after", rng)
            if m is None or part not in ident:
                continue
            sim = _simulate(m)
            after.append(sim["status"] == "ran"
                         and _same_response(sim["final"], ident[part]["final"]))
        before = [outcome(r) == "same" for r in records if r["mutation"] == "shadow"]
        summary["precedence"] = {
            "n": len(after),
            "stub_after_lib_reproduces_vendor": sum(after),
            "stub_before_lib_reproduces_vendor": sum(before),
        }
    (dest / "summary.json").write_text(json.dumps(summary, indent=2))

    print(f"held-out parts: {len(parts)}   mutants: {len(records)}")
    print(f"{'mutation':<16}{'label':<8}{'n':>4}{'flagged':>9}{'rate':>8}")
    for kind, v in summary["by_mutation"].items():
        print(f"{kind:<16}{v['label']:<8}{v['n']:>4}{v['flagged']:>9}"
              f"{v['rate']:>7.0f}%")
    for name in ("full", "provenance_only"):
        m = summary[name]
        print(f"{name:<16} TP={m['tp']:>3} FP={m['fp']:>3} FN={m['fn']:>3} "
              f"TN={m['tn']:>3}  P={m['precision']:.0f}%  R={m['recall']:.0f}%")
    tex = Path(args.tex)
    tex.parent.mkdir(parents=True, exist_ok=True)
    full, prov = summary["full"], summary["provenance_only"]
    lines = ["% GENERATED by bench/mutants.py -- do not edit by hand.",
             f"% Regenerate: python bench/mutants.py --parts {args.parts}",
             rf"\newcommand{{\MutParts}}{{{summary['parts']}}}",
             rf"\newcommand{{\MutTotal}}{{{summary['records']}}}",
             rf"\newcommand{{\MutClasses}}{{{len(summary['by_mutation'])}}}",
             rf"\newcommand{{\MutTP}}{{{full['tp']}}}",
             rf"\newcommand{{\MutFP}}{{{full['fp']}}}",
             rf"\newcommand{{\MutFN}}{{{full['fn']}}}",
             rf"\newcommand{{\MutTN}}{{{full['tn']}}}",
             rf"\newcommand{{\MutPrecision}}{{{full['precision']:.0f}}}",
             rf"\newcommand{{\MutRecall}}{{{full['recall']:.0f}}}",
             rf"\newcommand{{\MutProvPrecision}}{{{prov['precision']:.0f}}}",
             rf"\newcommand{{\MutProvRecall}}{{{prov['recall']:.0f}}}"]
    pvn = summary["provenance_vs_naive"]
    for key, tag in (("destroying_n", "DestroyN"), ("valid_n", "ValidN"),
                     ("prov_caught", "ProvCaught"),
                     ("prov_valid_flagged", "ProvValidFlagged"),
                     ("naive_caught", "NaiveCaught"),
                     ("naive_valid_flagged", "NaiveValidFlagged")):
        lines.append(rf"\newcommand{{\Mut{tag}}}{{{pvn[key]}}}")
    for b, tag in (("name_include", "BaseName"), ("strict_names", "BaseStrict"),
                   ("direct_digest", "BaseDigest")):
        v = summary["baselines"][b]
        lines.append(rf"\newcommand{{\Mut{tag}Missed}}{{{v['invalid_accepted']}}}")
        lines.append(rf"\newcommand{{\Mut{tag}ValidKept}}{{{v['valid_accepted']}}}")
    prec = summary.get("precedence")
    if prec:
        lines.append(rf"\newcommand{{\PrecN}}{{{prec['n']}}}")
        lines.append(rf"\newcommand{{\PrecAfterVendor}}{{{prec['stub_after_lib_reproduces_vendor']}}}")
        lines.append(rf"\newcommand{{\PrecBeforeVendor}}{{{prec['stub_before_lib_reproduces_vendor']}}}")
    for kind, v in summary.get("ltspice_oracle", {}).items():
        tag = "".join(w.capitalize() for w in kind.split("_"))
        for key, name in (("same", "Same"), ("changed", "Changed"),
                          ("missing_model", "Missing"), ("did_not_run", "NoRun")):
            lines.append(rf"\newcommand{{\Oracle{tag}{name}}}{{{v['outcome'].get(key, 0)}}}")
    for kind, v in summary["by_mutation"].items():
        tag = "".join(w.capitalize() for w in kind.split("_"))
        lines.append(rf"\newcommand{{\Mut{tag}N}}{{{v['n']}}}")
        lines.append(rf"\newcommand{{\Mut{tag}Flagged}}{{{v['flagged']}}}")
        lines.append(rf"\newcommand{{\Mut{tag}Rate}}{{{v['rate']:.0f}}}")
    rows = []
    for kind, v in summary["by_mutation"].items():
        label = "valid" if v["label"] == "valid" else "invalid"
        rows.append(rf"\texttt{{{kind.replace('_', chr(92)+'_')}}} & {label} & "
                    rf"{v['n']} & {v['flagged']} & {v['rate']:.0f}\% \\")
    tbl = (r"""\begin{table}[t]
\centering
\caption{Held-out validation. """ + str(summary["parts"]) + r""" vendor parts
  that appear in no probe case, each reference netlist mutated into
  """ + str(len(summary["by_mutation"])) + r""" variants whose label follows
  from the edit rather than from judgement. \emph{Flagged} is the full
  Layer-1 audit. The two \emph{valid} rows are the test that matters for
  precision: \texttt{wrapper} instantiates the genuine part inside a locally
  defined pass-through, so it reads as a local definition while preserving
  provenance.}
\label{tab:mutants}
\small
\begin{tabular}{@{}llccc@{}}
\toprule
\textbf{Mutation} & \textbf{Label} & \textbf{$n$} & \textbf{Flagged} &
  \textbf{Rate} \\
\midrule
""" + "\n".join(rows) + r"""
\bottomrule
\end{tabular}
\end{table}
""")
    tex.write_text("\n".join(lines) + "\n\n" + tbl)
    print(f"wrote {dest/'mutants.jsonl'}, {dest/'summary.json'} and {tex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
