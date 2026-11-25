"""Per-case table: definition, tolerance and per-arm outcome.

Lists the cases a reader needs in order to judge whether
the suite is adversarial in the way the paper says it is, and to locate any
individual result in the released corpus. Everything here is derived from
`bench/cases.py` and the stored run, so it cannot drift from the numbers in
the body.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyze import load                     # noqa: E402
from cases import ALL_CASES                  # noqa: E402

ARMS = ("bare", "library", "requirement")
TEX_ESCAPE = str.maketrans({"_": r"\_", "&": r"\&", "%": r"\%", "#": r"\#"})


def esc(text: str) -> str:
    return str(text).translate(TEX_ESCAPE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id")
    ap.add_argument("--out", default="generated/case_rows.tex")
    args = ap.parse_args()

    out_dir, rows = load(args.run_id)
    rows = [r for r in rows if r.get("prompt_variant", "bare") in ARMS]
    verdicts = {}
    vpath = out_dir / "verdicts.jsonl"
    if vpath.exists():
        for line in vpath.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                verdicts[(d["case_id"], d["model"], d["sample"],
                          d.get("prompt_variant", "bare"))] = d["verdict"]

    def has_net(r):
        nf = r.get("netlist_file")
        return bool(nf) and (out_dir / nf).exists()

    from reconcile import no_completion_cause

    def fmt_target(name, target, tol):
        unit = "V" if name == "vout_dc" else "dB"
        sign = "$-$" if target < 0 else ""
        return rf"{sign}{abs(target):g} {unit} $\pm$ {tol:g}"

    order = ("bare", "requirement", "library")
    body, nocomp = [], []
    totals = {a: [0, 0, 0, 0] for a in order}   # net, att, pass, false
    # The table's caption states these two facts instead of spending a column.
    for case in ALL_CASES:
        want = "medium" if case.case_id.startswith("B") else "high"
        if case.fake_risk != want:
            raise SystemExit(f"{case.case_id}: caption says {want}-risk")
    for case in ALL_CASES:
        sub = [r for r in rows if r["case_id"] == case.case_id]
        scored = bool(case.metrics)
        target = (fmt_target(*case.metrics[0]) if scored
                  else r"\emph{unscored}")
        part = rf"\texttt{{{esc(case.part)}}}"
        if case.case_id == "D5":
            part += r"$^{\dagger}$"
        cells = []
        for a in order:
            arm_rows = [r for r in sub if r.get("prompt_variant", "bare") == a]
            net = [r for r in arm_rows if has_net(r)]
            npass = sum(1 for r in net if r.get("sim_pass"))
            graded = [r for r in net if r.get("sim_pass") and
                      (r["case_id"], r["model"], r["sample"], a) in verdicts]
            nfalse = sum(1 for r in graded if verdicts[
                (r["case_id"], r["model"], r["sample"], a)] == "FAKE")
            if scored:
                t = totals[a]
                t[0] += len(net); t[1] += len(arm_rows); t[2] += npass
                t[3] += nfalse
            p_cell = str(npass) if scored else "---"
            if len(arm_rows) != 9:
                raise SystemExit(f"{case.case_id} {a}: caption says nine slots")
            if a == "library" and (nfalse or (scored and len(graded) != npass)):
                raise SystemExit(f"{case.case_id}: caption says no false library pass")
            if a != "bare":
                cells.append(rf"{len(net)} & {p_cell}")
                continue
            # `---` means no numeric pass to grade, never zero false ones.
            f_cell = (str(nfalse) if scored and npass and len(graded) == npass
                      else "---")
            cells.append(rf"{len(net)} & {p_cell} & {f_cell}")
        body.append(rf"{case.case_id} & {part} & {target} & "
                    + " & ".join(cells) + r" \\")
    tb, tr, tl = totals["bare"], totals["requirement"], totals["library"]
    body.append(r"\midrule")
    body.append(rf"\multicolumn{{3}}{{@{{}}l}}{{\emph{{Scored total (19 cases)}}}} & "
                rf"{tb[0]} & {tb[2]} & {tb[3]} & {tr[0]} & {tr[2]} & "
                rf"{tl[0]} & {tl[2]} \\")

    short = {"anthropic/claude-opus-5": "Claude Opus 5",
             "openai/gpt-5.6-sol": "GPT-5.6 Sol",
             "google/gemini-3.1-pro-preview": "Gemini 3.1 Pro"}
    for a in order:
        arm_rows = [r for r in rows if r.get("prompt_variant", "bare") == a
                    and not r.get("manual_grade_only")]
        for m in sorted({r["model"] for r in arm_rows}):
            mr = [r for r in arm_rows if r["model"] == m]
            lost = [r for r in mr if not has_net(r)]
            c = {k: sum(1 for r in lost if no_completion_cause(r) == k)
                 for k in ("content_filter", "billing", "network")}
            nocomp.append(rf"\arm{{{a}}} &{short.get(m, esc(m))} & {len(mr)} & "
                          rf"{c['content_filter']} & {c['billing']} & {c['network']} \\")

    tex = ("% GENERATED by bench/case_table.py -- do not edit by hand.\n"
           "% Rows for the per-case table (arm order: BARE, REQUIREMENT, LIBRARY).\n"
           + "\n".join(body) + "\n")
    nocomp_tex = ("% GENERATED by bench/case_table.py -- do not edit by hand.\n"
                  "% Scored slots with no deck, by recorded cause.\n"
                  + "\n".join(nocomp) + "\n")
    Path(args.out).with_name("nocomp_rows.tex").write_text(nocomp_tex)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(tex)
    print(f"wrote {dest} ({len(ALL_CASES)} cases, {len(nocomp)} arm/model rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
