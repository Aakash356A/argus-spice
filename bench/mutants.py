"""Held-out validation: known-label provenance mutations on unseen parts.

The audit's predicates were written against the 20-case probe suite, so
reporting their precision and recall on that same suite measures fit, not
generalisation. This module builds an independent test set with labels that
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

No simulation and no API calls; every mutation is a text edit over files
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
from detect import _part_stem, _same_part, _significant_lines  # noqa: E402
from provenance import resolve                                 # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "data" / "templates"
SEED = 20260820

FAKE_MUTATIONS = ("substitute", "renamed_stub", "shadow", "drop_library",
                  "remove_part", "alias_file", "pin_permute")
VALID_MUTATIONS = ("identity", "wrapper")


def _instance_lines(netlist: str, part: str) -> list[str]:
    out = []
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("*") or line[:1].upper() != "X":
            continue
        tokens = line.split()
        if any(_same_part(t, part) for t in tokens[2:]):
            out.append(raw)
    return out


def _lib_lines(netlist: str, part: str) -> list[str]:
    out = []
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line.lower().startswith((".lib", ".include", ".inc")) \
           and "standard." not in line.lower():
            out.append(raw)
    return out


def _stub(part: str) -> str:
    """A behavioural stub of the shape the corpus actually contains: a name,
    a pin list, and controlled sources rather than device models."""
    return (f".subckt {part} 1 2 3 4 5 6 7 8\n"
            f"E1 8 0 VALUE={{V(2)-V(3)}}\n"
            f"R1 8 0 1meg\n"
            f".ends {part}\n")


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
        nodes = inst.split()[1:-1]
        wrapper_name = f"{_part_stem(part)}_WRAP"
        inner = " ".join(f"n{i}" for i in range(len(nodes)))
        body = (f".subckt {wrapper_name} {inner}\n"
                f"XINNER {inner} {part}\n"
                f".ends {wrapper_name}\n")
        replaced = netlist.replace(inst, inst.rsplit(None, 1)[0] + f" {wrapper_name}")
        return replaced.replace(".end", body + ".end", 1)

    if kind == "substitute":
        if not libs:
            return None
        out = netlist
        for lib in libs:
            out = out.replace(lib, "* library directive removed by mutation")
        return out.replace(".end", _stub(part) + ".end", 1)

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
            out = out.replace(inst, inst.rsplit(None, 1)[0] + f" {variant}")
        return out.replace(".end", _stub(variant) + ".end", 1)

    if kind == "shadow":
        # The directive that a naive check looks for is still present; a local
        # definition of the same name shadows it.
        if not libs:
            return None
        return netlist.replace(".end", _stub(part) + ".end", 1)

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
        # The directive resolves, but to a file that defines a different part.
        if not libs:
            return None
        others = [p.name for p in TEMPLATES.glob("*.net")
                  if not _same_part(p.stem, part)]
        if not others:
            return None
        decoy = rng.choice(others)
        out = netlist
        for lib in libs:
            head = lib.split()[0]
            out = out.replace(lib, f"{head} {decoy}")
        return out

    if kind == "pin_permute":
        if not instances:
            return None
        inst = instances[0]
        tokens = inst.split()
        nodes = tokens[1:-1]
        if len(nodes) < 2:
            return None
        i, j = 0, len(nodes) - 1
        nodes[i], nodes[j] = nodes[j], nodes[i]
        return netlist.replace(inst, " ".join([tokens[0]] + nodes + [tokens[-1]]))

    raise ValueError(f"unknown mutation {kind}")


def held_out_parts(limit: int, rng: random.Random) -> list[tuple[str, Path]]:
    """Parts with a reference netlist that appear in no probe case."""
    seen_stems = {_part_stem(c.part) for c in ALL_CASES}
    candidates = []
    for path in sorted(TEMPLATES.glob("*.net")):
        stem = _part_stem(path.stem)
        if not stem or stem in seen_stems:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Usable only if the reference itself is genuine on this machine:
        # the part is instantiated and its library resolves to a vendor model.
        if not resolve(text, path.stem).vendor_supplied:
            continue
        candidates.append((path.stem, path))
        seen_stems.add(stem)
    rng.shuffle(candidates)
    return candidates[:limit]


def audit_flags(netlist: str, part: str, reference: str) -> dict:
    """The two gating predicates, re-derived exactly as the paper reports them."""
    from detect import pin_role_mismatch
    res = resolve(netlist, part)
    return {
        "provenance_fail": not res.vendor_supplied,
        "pin_role_fail": pin_role_mismatch(netlist, part, reference),
        "binding": res.binding,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parts", type=int, default=40,
                    help="held-out parts to draw (default 40)")
    ap.add_argument("--out", default="bench/runs/mutants")
    ap.add_argument("--tex", default="generated/mutants.tex",
                    help="where to write the LaTeX macro block")
    args = ap.parse_args()

    rng = random.Random(SEED)
    parts = held_out_parts(args.parts, rng)
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
            label = "valid" if kind in VALID_MUTATIONS else "fake"
            flags = audit_flags(mutated, part, reference)
            records.append({
                "part": part, "reference": path.name, "mutation": kind,
                "label": label, **flags,
                "flagged": bool(flags["provenance_fail"] or flags["pin_role_fail"]),
                "flagged_provenance_only": bool(flags["provenance_fail"]),
            })

    (dest / "mutants.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")

    def matrix(key):
        tp = sum(1 for r in records if r[key] and r["label"] == "fake")
        fp = sum(1 for r in records if r[key] and r["label"] == "valid")
        fn = sum(1 for r in records if not r[key] and r["label"] == "fake")
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
    for kind, v in summary["by_mutation"].items():
        tag = "".join(w.capitalize() for w in kind.split("_"))
        lines.append(rf"\newcommand{{\Mut{tag}N}}{{{v['n']}}}")
        lines.append(rf"\newcommand{{\Mut{tag}Flagged}}{{{v['flagged']}}}")
        lines.append(rf"\newcommand{{\Mut{tag}Rate}}{{{v['rate']:.0f}}}")
    rows = []
    for kind, v in summary["by_mutation"].items():
        label = "valid" if v["label"] == "valid" else "fake"
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
