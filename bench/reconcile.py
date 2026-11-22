"""Single source of truth for every number reported in the paper.

Usage:
    python bench/reconcile.py 20260809T211634Z [--out DIR]

Re-derives every headline figure from the stored corpus (never from a cached
audit field -- it reuses analyze.load(), which re-audits each netlist from disk),
partitions all 360 generations into mutually exclusive status categories that
must sum exactly, computes clustered bootstrap confidence intervals, and writes:

    numbers.tex   \newcommand macros for every headline number in the paper
    tables.tex    the generated LaTeX tables
    reconcile.json machine-readable dump of everything

Fails loudly (SystemExit non-zero) if any integrity assertion breaks: totals not
summing to 360, a zero denominator, a duplicate generation id, a reported
numeric pass with no simulator artifact, or conflicting grading labels.

No API calls. No network. Read-only over the run directory.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyze import load  # noqa: E402  (re-audits netlists from disk)
import ablation  # noqa: E402  (ladder, leave-one-out, Pass@k)
from cases import BY_ID  # noqa: E402
from provenance import resolve  # noqa: E402  (transitive include resolution)

BOOTSTRAP_ITERS = 10000
BOOTSTRAP_SEED = 20260813


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------
class IntegrityError(AssertionError):
    pass


_failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        _failures.append(message)


def pct(num: int, den: int) -> float:
    if den == 0:
        raise IntegrityError(f"zero denominator computing {num}/0")
    return 100.0 * num / den


def frac(num: int, den: int) -> str:
    """Explicit numerator/denominator rendering, per the reporting rule."""
    return f"{num}/{den} ({pct(num, den):.0f}\\%)"


# --------------------------------------------------------------------------
# clustered bootstrap
# --------------------------------------------------------------------------
def clustered_bootstrap_ci(rows, numerator_fn, denominator_fn,
                           cluster_key="case_id", iters=BOOTSTRAP_ITERS,
                           seed=BOOTSTRAP_SEED):
    """95% CI for a rate, resampling whole clusters (cases) with replacement.

    Generations are nested within case (and within model), so treating each
    generation as independent understates uncertainty. We resample cases.
    """
    clusters = defaultdict(list)
    for r in rows:
        clusters[r.get(cluster_key)].append(r)
    keys = list(clusters)
    if not keys:
        return (float("nan"), float("nan"))

    rng = random.Random(seed)
    estimates = []
    for _ in range(iters):
        num = den = 0
        for _ in range(len(keys)):
            grp = clusters[keys[rng.randrange(len(keys))]]
            num += sum(1 for r in grp if numerator_fn(r))
            den += sum(1 for r in grp if denominator_fn(r))
        if den:
            estimates.append(100.0 * num / den)
    if not estimates:
        return (float("nan"), float("nan"))
    estimates.sort()
    lo = estimates[int(0.025 * len(estimates))]
    hi = estimates[int(0.975 * len(estimates)) - 1]
    return (lo, hi)


# --------------------------------------------------------------------------
# helpers over rows
# --------------------------------------------------------------------------
def audit_of(r):
    return r.get("audit") or {}


def is_genuine(r):
    """A vendor-library reference: a .lib/.include directive is present and no
    local subcircuit shadows the requested part. This is the paper's "Ref."
    count; whether the reference resolves is `resolution_of`'s question.
    Matches analyze.py."""
    a = audit_of(r)
    return bool(a.get("has_library")) and not a.get("substituted")


def arm(r):
    return r.get("prompt_variant", "bare")


_resolution_cache: dict[str, object] = {}


def resolution_of(r, out_dir):
    """Transitive-include resolution for one row's netlist, memoised.

    `is_genuine` above is a *structural* claim -- a library directive is
    present and no local subcircuit shadows the part -- and deliberately does
    not assert that the directive resolved. This is the stronger claim: the
    part name binds to exactly one vendor-supplied definition on this machine,
    with the resolved file's SHA-256 recorded. The two are reported side by
    side rather than one replacing the other, because the gap between them is
    itself a result.
    """
    nf = r.get("netlist_file")
    if not nf:
        return None
    if nf not in _resolution_cache:
        case = BY_ID.get(r.get("case_id"))
        if case is None:
            return None
        text = (out_dir / nf).read_text(encoding="utf-8", errors="ignore")
        _resolution_cache[nf] = resolve(text, case.part)
    return _resolution_cache[nf]


def is_resolved_vendor(r, out_dir):
    res = resolution_of(r, out_dir)
    return bool(res and res.vendor_supplied)


def no_completion_cause(r) -> str:
    """Why a slot returned no netlist, read from the recorded error string.

    The recorded status names what the router returned, not the upstream
    policy behind it, so only `content_filter` finish reasons are called
    filtering; billing and transport failures are reported as such.
    """
    err = (r.get("error") or "").lower()
    if "content_filter" in err:
        return "content_filter"
    if "http 402" in err or "payment required" in err:
        return "billing"
    if any(s in err for s in ("connection", "timed out", "timeout", "reset")):
        return "network"
    return "other"


def missing_reverification(qualified_keys, reverify) -> list:
    """Qualified keys with no re-verification record whose control passed.

    Every provenance-qualified pass must be covered; a missing or failed
    control is a build failure, not a skipped row."""
    return [k for k in qualified_keys
            if (reverify.get(k) or {}).get("control") != "met"]


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def review_macros(*, corpus_rows, corpus_scored, numeric_pass, netlist_rows,
                  key_of, verdicts_full, is_fake, reverify, out_dir,
                  pred_by_key) -> list[str]:
    """Denominators, loss causes, coverage and sensitivity figures.

    Every figure here is something a reader asked to see stated explicitly;
    each is derived from the stored records and asserted where a count must
    reconcile with another.
    """
    out: list[str] = []
    tag = {"bare": "Bare", "requirement": "Req", "library": "Lib"}

    def has_net(r):
        return bool(r.get("netlist_file")) and (out_dir / r["netlist_file"]).exists()

    # --- no-completion causes ------------------------------------------
    causes_all = Counter(no_completion_cause(r) for r in corpus_rows if not has_net(r))
    check(causes_all.get("other", 0) == 0,
          f"unclassified no-completion cause(s): {causes_all}")
    for scope, pool in (("Scored", corpus_scored), ("All", corpus_rows)):
        for c in ("content_filter", "billing", "network"):
            ctag = {"content_filter": "Filter", "billing": "Billing",
                    "network": "Network"}[c]
            n = sum(1 for r in pool if not has_net(r) and no_completion_cause(r) == c)
            out.append(rf"\newcommand{{\NoComp{scope}{ctag}}}{{{n}}}")
            for a, at in tag.items():
                n = sum(1 for r in pool if arm(r) == a and not has_net(r)
                        and no_completion_cause(r) == c)
                out.append(rf"\newcommand{{\NoComp{scope}{ctag}{at}}}{{{n}}}")
        out.append(rf"\newcommand{{\NoComp{scope}Total}}{{"
                   rf"{sum(1 for r in pool if not has_net(r))}}}")
    claude_filter = Counter(arm(r) for r in corpus_scored if not has_net(r)
                            and no_completion_cause(r) == "content_filter"
                            and "claude" in r["model"])
    check(sum(claude_filter.values()) == sum(
        1 for r in corpus_scored if not has_net(r)
        and no_completion_cause(r) == "content_filter"),
          "a content-filter return came from a model other than Claude")

    # --- reference / resolution denominators ----------------------------
    for a, at in tag.items():
        att = [r for r in corpus_scored if arm(r) == a]
        net = [r for r in att if has_net(r)]
        ref = [r for r in net if is_genuine(r)]
        res = [r for r in net if is_resolved_vendor(r, out_dir)]
        check(set(map(key_of, res)) <= set(map(key_of, net)),
              "a resolved row has no netlist")
        out.append(rf"\newcommand{{\Res{at}OfRef}}{{"
                   rf"{pct(sum(1 for r in ref if is_resolved_vendor(r, out_dir)), len(ref)):.0f}}}")
        out.append(rf"\newcommand{{\Res{at}OfRefN}}{{"
                   rf"{sum(1 for r in ref if is_resolved_vendor(r, out_dir))}}}")
        out.append(rf"\newcommand{{\Ref{at}Att}}{{{pct(len(ref), len(att)):.0f}}}")
        out.append(rf"\newcommand{{\Res{at}Att}}{{{pct(len(res), len(att)):.0f}}}")

    # --- numeric misses with no finite measurement ----------------------
    misses = [r for r in netlist_rows if r.get("converged") and not r.get("sim_pass")]
    nomeas = [r for r in misses
              if not any(_finite(rep.get("measured")) for rep in r.get("reports") or [])]
    out.append(rf"\newcommand{{\NumericFailNoMeasure}}{{{len(nomeas)}}}")
    out.append(rf"\newcommand{{\NumericFailFinite}}{{{len(misses) - len(nomeas)}}}")

    # --- re-verification completeness and coverage ----------------------
    qualified = [r for r in numeric_pass if is_resolved_vendor(r, out_dir)]
    missing = missing_reverification([key_of(r) for r in qualified], reverify)
    check(not missing,
          f"{len(missing)} provenance-qualified pass(es) lack a passing "
          f"re-verification record: {missing[:3]}")
    recs = [reverify[key_of(r)] for r in qualified]
    outcomes = Counter(d.get("delete_outcome") for d in recs)
    check(set(outcomes) <= {"finite_miss", "zero_output", "no_measure",
                            "no_sim", "still_met"},
          f"unknown deletion outcome(s): {set(outcomes)}")
    check(outcomes.get("still_met", 0) == 0,
          "a qualified pass still meets its target with the part deleted")
    del_finite = outcomes.get("finite_miss", 0)
    del_nan = outcomes.get("zero_output", 0)
    del_other = outcomes.get("no_measure", 0) + outcomes.get("no_sim", 0)
    lsq = [d["thd_lsq"] for d in recs if _finite(d.get("thd_lsq"))]
    fft = [d["thd_fft"] for d in recs if _finite(d.get("thd_fft"))]
    out += [rf"\newcommand{{\RevQualified}}{{{len(qualified)}}}",
            rf"\newcommand{{\RevDeleteFinite}}{{{del_finite}}}",
            rf"\newcommand{{\RevDeleteNaN}}{{{del_nan}}}",
            rf"\newcommand{{\RevDeleteOther}}{{{del_other}}}",
            rf"\newcommand{{\RevTHDLsqN}}{{{len(lsq)}}}",
            rf"\newcommand{{\RevTHDFftN}}{{{len(fft)}}}",
            rf"\newcommand{{\RevMaxTHDLsq}}{{{max(lsq, default=0):.4f}}}",
            rf"\newcommand{{\RevMaxTHDFft}}{{{max(fft, default=0):.4f}}}"]

    # --- grade history and grading basis --------------------------------
    changed = [r for r in numeric_pass
               if verdicts_full.get(key_of(r), {}).get("previous_verdict")]
    for a, at in (("bare", "Bare"), ("library", "Lib")):
        out.append(rf"\newcommand{{\Regraded{at}}}{{"
                   rf"{sum(1 for r in changed if arm(r) == a)}}}")
        prev_false = sum(1 for r in numeric_pass if arm(r) == a and (
            is_fake(r) or verdicts_full.get(key_of(r), {}).get("previous_verdict") == "FAKE"))
        out.append(rf"\newcommand{{\PrevFalse{at}}}{{{prev_false}}}")
    out.append(rf"\newcommand{{\PrevFalseTotal}}{{"
               rf"{sum(1 for r in numeric_pass if is_fake(r) or verdicts_full.get(key_of(r), {}).get('previous_verdict') == 'FAKE')}}}")
    audit_notes = sum(1 for r in numeric_pass
                      if "audit" in (verdicts_full.get(key_of(r), {}).get("note") or "").lower())
    out.append(rf"\newcommand{{\GradeNotesAudit}}{{{audit_notes}}}")

    # --- REQUIREMENT grades (mechanical) -------------------------------
    req_pass = [r for r in corpus_scored if arm(r) == "requirement" and r.get("sim_pass")]
    req_graded = [r for r in req_pass if key_of(r) in verdicts_full]
    check(len(req_graded) == len(req_pass), "a REQUIREMENT pass is ungraded")
    req_missing = missing_reverification(
        [key_of(r) for r in req_pass if is_resolved_vendor(r, out_dir)], reverify)
    check(not req_missing,
          f"{len(req_missing)} qualified REQUIREMENT pass(es) lack a passing "
          f"re-verification record: {req_missing[:3]}")
    out.append(rf"\newcommand{{\CasesWithPassReq}}{{{len({r['case_id'] for r in req_pass})}}}")
    out.append(rf"\newcommand{{\RevAllQualified}}{{"
               rf"{sum(1 for r in corpus_scored if r.get('sim_pass') and is_resolved_vendor(r, out_dir))}}}")
    out.append(rf"\newcommand{{\ReqPass}}{{{len(req_pass)}}}")
    out.append(rf"\newcommand{{\ReqFalse}}{{"
               rf"{sum(1 for r in req_graded if verdicts_full[key_of(r)].get('verdict') == 'FAKE')}}}")

    # --- pin-role flags among numeric passes ----------------------------
    flagged = [r for r in numeric_pass if pred_by_key[key_of(r)]["pin_role_mismatch"]]
    local = sum(1 for r in flagged if pred_by_key[key_of(r)]["substituted"])
    qual = [r for r in flagged if is_resolved_vendor(r, out_dir)]
    check(local + len(qual) == len(flagged),
          "a pin-role-flagged pass is neither a local substitute nor qualified")
    out.append(rf"\newcommand{{\PinRolePassLocal}}{{{local}}}")
    out.append(rf"\newcommand{{\PinRolePassQualified}}{{{len(qual)}}}")

    # --- sensitivity ----------------------------------------------------
    def false_share(pool):
        return sum(1 for r in pool if is_fake(r)), len(pool)

    bare_pass = [r for r in numeric_pass if arm(r) == "bare"]
    lib_pass = [r for r in numeric_pass if arm(r) == "library"]
    for name, pool in (("Bare", bare_pass), ("Lib", lib_pass)):
        out.append(rf"\newcommand{{\CasesWithPass{name}}}{{"
                   rf"{len({r['case_id'] for r in pool})}}}")
    for risk in ("high", "medium"):
        f, n = false_share([r for r in bare_pass if r.get("fake_risk") == risk])
        rt = risk.capitalize()
        out.append(rf"\newcommand{{\BareFalse{rt}}}{{{f}}}")
        out.append(rf"\newcommand{{\BarePass{rt}}}{{{n}}}")
    f, n = false_share([r for r in bare_pass if r["case_id"] != "C1"])
    out.append(rf"\newcommand{{\BareFalseNoCOne}}{{{f}}}")
    out.append(rf"\newcommand{{\BarePassNoCOne}}{{{n}}}")
    # The three cases with known definition defects (C1 supply, D3
    # single-node gain, D5 infeasible buffer) excluded together.
    defects = {"C1", "D3", "D5"}
    f, n = false_share([r for r in bare_pass if r["case_id"] not in defects])
    slots = [r for r in corpus_scored if arm(r) == "bare" and r["case_id"] not in defects]
    num = sum(1 for r in slots if r.get("sim_pass"))
    qual = sum(1 for r in slots if r.get("sim_pass") and is_resolved_vendor(r, out_dir))
    out += [rf"\newcommand{{\BareFalseNoDefect}}{{{f}}}",
            rf"\newcommand{{\BarePassNoDefect}}{{{n}}}",
            rf"\newcommand{{\BareSlotsNoDefect}}{{{len(slots)}}}",
            rf"\newcommand{{\BareNumNoDefect}}{{{num}}}",
            rf"\newcommand{{\BareQualNoDefect}}{{{qual}}}",
            rf"\newcommand{{\BareDropNoDefect}}{{{pct(num - qual, len(slots)):.1f}}}"]
    lo, hi = clustered_bootstrap_ci(bare_pass, is_fake, lambda r: True,
                                    cluster_key="part")
    out.append(rf"\newcommand{{\BareFalsePartCILo}}{{{lo:.0f}}}")
    out.append(rf"\newcommand{{\BareFalsePartCIHi}}{{{hi:.0f}}}")
    lopo = []
    for p in sorted({r["part"] for r in bare_pass}):
        f, n = false_share([r for r in bare_pass if r["part"] != p])
        lopo.append(pct(f, n))
    out.append(rf"\newcommand{{\BareFalseLopoMin}}{{{min(lopo):.0f}}}")
    out.append(rf"\newcommand{{\BareFalseLopoMax}}{{{max(lopo):.0f}}}")
    for a, name in (("bare", "Bare"), ("library", "Lib")):
        att = [r for r in corpus_scored if arm(r) == a and "claude" not in r["model"]]
        good = sum(1 for r in numeric_pass if arm(r) == a
                   and "claude" not in r["model"] and not is_fake(r))
        out.append(rf"\newcommand{{\NoClaude{name}Valid}}{{{good}}}")
        out.append(rf"\newcommand{{\NoClaude{name}Att}}{{{len(att)}}}")
    # --- scored denominators and Pass@3 cell counts ----------------------
    gl = [r for r in corpus_scored if arm(r) in ("bare", "library")]
    out.append(rf"\newcommand{{\ScoredBareLib}}{{{len(gl)}}}")
    out.append(rf"\newcommand{{\DecksScored}}{{{sum(1 for r in corpus_scored if has_net(r))}}}")
    for a, at in tag.items():
        cells: dict = {}
        for r in corpus_scored:
            if arm(r) != a:
                continue
            c = cells.setdefault((r["model"], r["case_id"]), [False, False])
            if r.get("sim_pass"):
                c[0] = True
                if is_resolved_vendor(r, out_dir):
                    c[1] = True
        out.append(rf"\newcommand{{\Cells{at}}}{{{len(cells)}}}")
        out.append(rf"\newcommand{{\CellsNum{at}}}{{{sum(v[0] for v in cells.values())}}}")
        out.append(rf"\newcommand{{\CellsQual{at}}}{{{sum(v[1] for v in cells.values())}}}")
        att = sum(1 for r in corpus_scored if arm(r) == a)
        num = sum(1 for r in corpus_scored if arm(r) == a and r.get("sim_pass"))
        qual = sum(1 for r in corpus_scored if arm(r) == a and r.get("sim_pass")
                   and is_resolved_vendor(r, out_dir))
        out.append(rf"\newcommand{{\SlotsNum{at}}}{{{num}}}")
        out.append(rf"\newcommand{{\SlotsQual{at}}}{{{qual}}}")
        out.append(rf"\newcommand{{\SlotsAtt{at}}}{{{att}}}")

    out.extend(experiment_macros())
    return out


def experiment_macros() -> list[str]:
    """Macros for the fixture, collateral and timing experiments, read from
    their stored outputs (bench/fixtures.py, bench/collateral.py)."""
    runs = Path(__file__).resolve().parent / "runs"
    out: list[str] = []
    fx_path = runs / "fixtures" / "fixtures.jsonl"
    if fx_path.exists():
        fx = [json.loads(l) for l in fx_path.read_text().splitlines() if l.strip()]
        fake = [r for r in fx if r["grade"] == "FAKE"]
        real = [r for r in fx if r["grade"] == "REAL"]
        ungr = [r for r in fx if r["grade"] not in ("FAKE", "REAL")]
        check(all(r["status"] in ("ran", "unsupported", "no_sim") for r in fx),
              "unknown fixture status")
        out += [rf"\newcommand{{\FixN}}{{{len(fx)}}}",
                rf"\newcommand{{\FixCases}}{{{len({r['case_id'] for r in fx})}}}",
                rf"\newcommand{{\FixFakeN}}{{{len(fake)}}}",
                rf"\newcommand{{\FixFakePass}}{{{sum(1 for r in fake if r.get('all'))}}}",
                rf"\newcommand{{\FixRealN}}{{{len(real) + len(ungr)}}}",
                rf"\newcommand{{\FixRealPass}}{{{sum(1 for r in real + ungr if r.get('all'))}}}",
                rf"\newcommand{{\FixUnsupported}}{{{sum(1 for r in fx if r['status'] != 'ran')}}}",
                rf"\newcommand{{\FixFakeQualified}}{{{sum(1 for r in fake if r['qualified'])}}}",
                rf"\newcommand{{\FixRealQualified}}{{{sum(1 for r in real + ungr if r['qualified'])}}}"]
        d1 = [r for r in fx if r["case_id"] == "D1" and r.get("release_status") == "ran"]
        def stage(sub):
            return f"{sum(1 for r in sub if r.get('stage_ok'))}/{len(sub)}"
        out += [rf"\newcommand{{\StageFake}}{{{stage([r for r in d1 if r['grade'] == 'FAKE'])}}}",
                rf"\newcommand{{\StageLib}}{{{stage([r for r in d1 if r['grade'] != 'FAKE' and r['arm'] == 'library'])}}}",
                rf"\newcommand{{\StageReq}}{{{stage([r for r in d1 if r['arm'] == 'requirement'])}}}",
                rf"\newcommand{{\StageFakeSurvive}}{{{sum(1 for r in d1 if r['grade'] == 'FAKE' and r.get('stage_ok'))}}}"]
        for cid in sorted({r["case_id"] for r in fx}):
            sub = [r for r in fx if r["case_id"] == cid]
            f = [r for r in sub if r["grade"] == "FAKE"]
            g = [r for r in sub if r["grade"] != "FAKE"]
            t = (cid.replace("1", "One").replace("3", "Three")
                 .replace("4", "Four"))
            out += [rf"\newcommand{{\Fix{t}Fake}}{{{sum(1 for r in f if r.get('all'))}/{len(f)}}}",
                    rf"\newcommand{{\Fix{t}Real}}{{{sum(1 for r in g if r.get('all'))}/{len(g)}}}"]
    col_path = runs / "collateral" / "summary.json"
    if col_path.exists():
        col = json.loads(col_path.read_text())
        v = [json.loads(l) for l in (runs / "collateral" / "variants.jsonl").read_text().splitlines() if l.strip()]
        same_file = [x for x in v if x["to"] in ("AD8091", "AD8552")]
        other = [x for x in v if x["to"] not in ("AD8091", "AD8552")]
        out += [rf"\newcommand{{\VarN}}{{{len(v)}}}",
                rf"\newcommand{{\VarSameFileN}}{{{len(same_file)}}}",
                rf"\newcommand{{\VarSameFilePass}}{{{sum(x['fixture_pass'] for x in same_file)}}}",
                rf"\newcommand{{\VarSameFileRejected}}{{{sum(not x['argus_accepts'] for x in same_file)}}}",
                rf"\newcommand{{\VarOtherN}}{{{len(other)}}}",
                rf"\newcommand{{\VarOtherNoSim}}{{{sum(x['fixture']['status'] == 'no_sim' for x in other)}}}",
                rf"\newcommand{{\VarOtherStemAccepts}}{{{sum(x['stem_check_accepts'] for x in other)}}}",
                rf"\newcommand{{\VarArgusRejects}}{{{col['variants']['argus_rejects']}}}",
                rf"\newcommand{{\OpaqueQualified}}{{{col['opaque']['qualified_passes']}}}",
                rf"\newcommand{{\OpaqueBindings}}{{{col['opaque']['distinct_bindings']}}}",
                rf"\newcommand{{\OpaqueExported}}{{{col['opaque']['exported']}}}",
                rf"\newcommand{{\PrecBenignN}}{{{col['precedence']['n']}}}",
                rf"\newcommand{{\PrecBenignAfter}}{{{col['precedence']['after_reproduces']}}}",
                rf"\newcommand{{\PrecBenignBeforeChanges}}{{{col['precedence']['before_changes']}}}",
                rf"\newcommand{{\PrecBenignIndist}}{{{len(col['precedence']['before_reproduces'])}}}",
                rf"\newcommand{{\PrecBenignFails}}{{{col['precedence']['before_fails']}}}"]
    sn_path = runs / "samename" / "summary.json"
    if sn_path.exists():
        sn = json.loads(sn_path.read_text())
        for kind, tag in (("transitive", "Trans"), ("path", "Path")):
            v = sn[kind]
            out += [rf"\newcommand{{\SN{tag}N}}{{{v['n']}}}",
                    rf"\newcommand{{\SN{tag}Fixture}}{{{v['fixture_pass']}}}",
                    rf"\newcommand{{\SN{tag}Bound}}{{{v['simulator_bound_substitute']}}}",
                    rf"\newcommand{{\SN{tag}Base}}{{{v['baseline_accepts']}}}",
                    rf"\newcommand{{\SN{tag}Strict}}{{{v['strict_baseline_accepts']}}}",
                    rf"\newcommand{{\SN{tag}Argus}}{{{v['argus_rejects']}}}"]
        for cid in ("B1", "B4"):
            w = sn["sweep"][cid]
            t = cid.replace("1", "One").replace("4", "Four")
            out += [rf"\newcommand{{\SNSweep{t}N}}{{{w['alternatives']}}}",
                    rf"\newcommand{{\SNSweep{t}Pass}}{{{w['pass_and_differ']}}}",
                    rf"\newcommand{{\SNAlt{t}}}{{{sn['chosen'][cid]}}}"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--out", default=None,
                    help="directory for numbers.tex/tables.tex "
                         "(default: <run>/paper)")
    args = ap.parse_args()

    out_dir, rows = load(args.run_id)
    dest = Path(args.out) if args.out else (out_dir / "paper")
    dest.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- partition
    # Scoped to bare/library: those are the two arms this paper's numbers
    # describe. results.jsonl is one file shared across every prompt-variant
    # sweep ever run under this run id, so as soon as a `requirement` or
    # `requirement` row lands in it (the three-arm ablation), an unscoped
    # "everything not manual_grade_only" denominator silently drifts -- the
    # abstract's \NumAttempted moved 360->361 the first time a single
    # off-scope smoke-test row was added, before this filter existed. Rows
    # from other variants get their own analysis once that experiment exists;
    # they must never blend into these macros by accident.
    rows = [r for r in rows if r.get("prompt_variant", "bare") in ("bare", "library")]
    total = len(rows)
    excluded = [r for r in rows if r.get("manual_grade_only")]
    analysed = [r for r in rows if not r.get("manual_grade_only")]

    def has_netlist(r):
        nf = r.get("netlist_file")
        return bool(nf) and (out_dir / nf).exists()

    no_completion = [r for r in analysed if not has_netlist(r)]
    netlists = [r for r in analysed if has_netlist(r)]

    converged = [r for r in netlists if r.get("converged")]
    not_converged = [r for r in netlists if not r.get("converged")]
    numeric_pass = [r for r in converged if r.get("sim_pass")]
    numeric_fail = [r for r in converged if not r.get("sim_pass")]

    # ------------------------------------------------------------- assertions
    check(len(excluded) + len(no_completion) + len(not_converged)
          + len(numeric_fail) + len(numeric_pass) == total,
          "status categories do not sum to the corpus total")
    check(all(r.get("status") == "LLM_ERROR" for r in no_completion),
          "a no-netlist row is not LLM_ERROR")
    check(all(has_netlist(r) for r in numeric_pass),
          "a reported numeric pass has no simulator artifact on disk")

    ids = [(r.get("case_id"), r.get("model"), r.get("sample"),
            r.get("prompt_variant", "bare")) for r in rows]
    dupes = [k for k, n in Counter(ids).items() if n > 1]
    check(not dupes, f"duplicate generation ids: {dupes[:5]}")

    # ------------------------------------------------------------------ guard
    guard_rejects_netlists = sum(1 for r in netlists if r.get("guard_would_reject"))
    guard_rejects_passes = sum(1 for r in numeric_pass if r.get("guard_would_reject"))
    guard_rejects_nocompletion = sum(1 for r in no_completion
                                     if r.get("guard_would_reject"))
    check(guard_rejects_nocompletion == 0,
          "guard claims to reject a generation that produced no netlist")

    # -------------------------------------------------------------- detectors
    flags = ["substituted", "behavioral_stub", "no_part", "pin_role_mismatch"]
    flag_counts = {f: sum(1 for r in netlists if audit_of(r).get(f)) for f in flags}

    pin_role = [r for r in netlists if audit_of(r).get("pin_role_mismatch")]
    pin_role_by_arm = {}
    for a in ("bare", "library"):
        sub = [r for r in pin_role if arm(r) == a]
        pin_role_by_arm[a] = {
            "total": len(sub),
            "converged": sum(1 for r in sub if r.get("converged")),
            "numeric_pass": sum(1 for r in sub if r.get("sim_pass")),
        }
    pin_role_pass_total = sum(v["numeric_pass"] for v in pin_role_by_arm.values())

    # pin-count mismatches are simulator-caught, read off the error string
    def is_pin_count(r):
        err = (r.get("error") or "").replace("\x00", "").lower()
        return "pin) count mismatch" in err or "port(pin) count mismatch" in err

    pin_count = sum(1 for r in netlists if is_pin_count(r))

    # --------------------------------------------------------------- grading
    verdict_path = out_dir / "verdicts.jsonl"
    verdicts = {}
    verdicts_basis = {}
    verdicts_full = {}
    conflicts = []
    if verdict_path.exists():
        for line in verdict_path.read_text().splitlines():
            if not line.strip():
                continue
            v = json.loads(line)
            key = (v.get("case_id"), v.get("model"), v.get("sample"),
                   v.get("prompt_variant", "bare"))
            if key in verdicts and verdicts[key] != v.get("verdict"):
                conflicts.append(key)
            verdicts[key] = v.get("verdict")
            verdicts_basis[key] = v.get("basis", "hand read")
            verdicts_full[key] = v
    check(not conflicts, f"conflicting grading labels for {conflicts[:5]}")

    def key_of(r):
        return (r.get("case_id"), r.get("model"), r.get("sample"),
                arm(r))

    # ------------------------------------------------------------------- arms
    per_arm = {}
    for a in ("bare", "library"):
        a_analysed = [r for r in analysed if arm(r) == a]
        a_net = [r for r in netlists if arm(r) == a]
        a_pass = [r for r in numeric_pass if arm(r) == a]
        # False acceptance is a property of the manual grade, not of the audit.
        # Deriving it from `audited_pass` scores the audit against itself and
        # does not sum to the graded total (70+1 != 79).
        a_false = [r for r in a_pass if verdicts.get(key_of(r)) == "FAKE"]
        a_genuine = [r for r in a_net if is_genuine(r)]
        lo, hi = clustered_bootstrap_ci(
            a_pass, lambda r: verdicts.get(key_of(r)) == "FAKE", lambda r: True)
        g_lo, g_hi = clustered_bootstrap_ci(
            a_net, is_genuine, lambda r: True)
        per_arm[a] = {
            "attempted": len(a_analysed),
            "netlists": len(a_net),
            "no_completion": len(a_analysed) - len(a_net),
            "numeric_pass": len(a_pass),
            "false_pass": len(a_false),
            "false_pass_rate": pct(len(a_false), len(a_pass)) if a_pass else 0.0,
            "false_pass_ci": (lo, hi),
            "genuine": len(a_genuine),
            "genuine_rate_over_netlists": pct(len(a_genuine), len(a_net)),
            "genuine_rate_over_attempted": pct(len(a_genuine), len(a_analysed)),
            "genuine_ci": (g_lo, g_hi),
            # unconditional, per AR-9
            "pass_per_attempted": pct(len(a_pass), len(a_analysed)),
            "false_per_attempted": pct(len(a_false), len(a_analysed)),
            # A pass that is both numerically correct and genuinely uses the
            # requested part -- the end-to-end yield a practitioner cares
            # about, and the only rate that survives both gates.
            "valid_pass": len(a_pass) - len(a_false),
            "valid_pass_per_attempted": pct(len(a_pass) - len(a_false),
                                            len(a_analysed)),
        }

    # ----------------------------------------------------------------- models
    per_model = {}
    for m in sorted({r["model"] for r in analysed}):
        entry = {}
        for a in ("bare", "library"):
            m_net = [r for r in netlists if r["model"] == m and arm(r) == a]
            m_all = [r for r in analysed if r["model"] == m and arm(r) == a]
            m_pass = [r for r in m_net if r.get("sim_pass")]
            # grade-based, matching the per-arm definition above
            m_false = [r for r in m_pass if verdicts.get(key_of(r)) == "FAKE"]
            entry[a] = {
                "attempted": len(m_all),
                "netlists": len(m_net),
                "numeric_pass": len(m_pass),
                "false_pass": len(m_false),
                "false_pass_rate": pct(len(m_false), len(m_pass)) if m_pass else None,
                "genuine": sum(1 for r in m_net if is_genuine(r)),
                "stub": sum(1 for r in m_net if audit_of(r).get("behavioral_stub")),
                "local_subckt": sum(1 for r in m_net if audit_of(r).get("substituted")),
                # Instantiated by name with neither a local definition nor any
                # .lib/.include: detect.unresolvable_part, from the stored audit.
                "unresolvable": sum(1 for r in m_net
                                    if audit_of(r).get("instantiated")
                                    and not audit_of(r).get("substituted")
                                    and not audit_of(r).get("has_library")),
            }
        per_model[m] = entry
    GEMINI = next(m for m in per_model if m.startswith("google/gemini"))


    linearity_path = out_dir / "linearity.jsonl"
    measured_keys = set()
    if linearity_path.exists():
        for line in linearity_path.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            measured_keys.add((d.get("case_id"), d.get("model"), d.get("sample"),
                               d.get("prompt_variant", "bare")))


    audit_flagged = [r for r in numeric_pass if not r.get("audited_pass")]
    audit_cleared = [r for r in numeric_pass if r.get("audited_pass")]

    graded_cleared = [r for r in audit_cleared if key_of(r) in verdicts]
    graded_fake = [r for r in graded_cleared if verdicts[key_of(r)] == "FAKE"]
    graded_real = [r for r in graded_cleared if verdicts[key_of(r)] == "REAL"]
    measured_cleared = [r for r in audit_cleared
                        if key_of(r) in measured_keys and key_of(r) not in verdicts]
    unresolved = [r for r in audit_cleared
                  if key_of(r) not in verdicts and key_of(r) not in measured_keys]

    check(len(graded_cleared) + len(measured_cleared) + len(unresolved)
          == len(audit_cleared),
          "cleared-netlist resolution categories do not partition")

    human_confirmed_fake = len(graded_fake)
    assumed_fake = len(audit_flagged)
    total_fake_assumed = assumed_fake + human_confirmed_fake

    # ---------------------------------------------------------------- matrix
    # Every numeric pass now carries an independent label, so the audit is
    # scored against ground truth rather than partly defining it. Requires a
    # verdict for every pass: a missing label would silently bias the matrix,
    # so the run fails instead.
    unlabelled = [r for r in numeric_pass if key_of(r) not in verdicts]
    check(not unlabelled,
          f"{len(unlabelled)} numeric pass(es) have no human verdict; "
          f"the confusion matrix would be computed over a biased subset")

    def _is_fake(r):
        return verdicts.get(key_of(r)) == "FAKE"

    def _flagged(r):
        return not r.get("audited_pass")

    tp = sum(1 for r in numeric_pass if _flagged(r) and _is_fake(r))
    fp = sum(1 for r in numeric_pass if _flagged(r) and not _is_fake(r))
    fn = sum(1 for r in numeric_pass if not _flagged(r) and _is_fake(r))
    tn = sum(1 for r in numeric_pass if not _flagged(r) and not _is_fake(r))
    check(tp + fp + fn + tn == len(numeric_pass),
          "confusion matrix does not partition the numeric passes")

    # ------------------------------------------------- behavioural layer
    # Layer 2's only corpus-safe signal is the waveform-integrity check.
    # multimetric.jsonl is NOT usable here: its own header records that
    # bandwidth_hz produced no value on any of the 35 gain passes (so
    # second_pass=False means "measurement failed", not "missed bandwidth")
    # and that quiescent_supply_a is dominated by load current and does not
    # separate a behavioural stub from the real part. Treating either as a
    # rejection criterion manufactures both detections and over-rejections.
    lin_path = out_dir / "linearity.jsonl"
    lin_rows = {}
    if lin_path.exists():
        for line in lin_path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                lin_rows[(d.get("case_id"), d.get("model"), d.get("sample"),
                          d.get("prompt_variant", "bare"))] = d

    def _wave_rejects(r):
        L = lin_rows.get(key_of(r))
        return bool(L and not L.get("skipped") and L.get("violation"))

    wave_evaluated = sum(1 for r in numeric_pass
                         if (lin_rows.get(key_of(r)) or {}).get("skipped") is None
                         and key_of(r) in lin_rows)
    wave_violations = sum(1 for r in numeric_pass if _wave_rejects(r))
    wave_new = sum(1 for r in numeric_pass
                   if _wave_rejects(r) and not _flagged(r))
    full = {
        "wave_evaluated": wave_evaluated,
        "wave_violations": wave_violations,
        "wave_beyond_audit": wave_new,
    }

    labelled_fake = tp + fn
    precision = pct(tp, tp + fp) if (tp + fp) else None
    recall = pct(tp, tp + fn) if (tp + fn) else None
    specificity = pct(tn, tn + fp) if (tn + fp) else None
    accuracy = pct(tp + tn, len(numeric_pass))
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else None)

    # False acceptance is now the human-labelled count, not the audit's own
    # output, so the CI resamples the labels rather than the assumption.
    fp_lo, fp_hi = clustered_bootstrap_ci(
        numeric_pass, _is_fake, lambda r: True)

    confusion = {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "labelled_fake": labelled_fake, "labelled_real": fp + tn,
        "precision": precision, "recall": recall,
        "specificity": specificity, "f1": f1, "accuracy": accuracy,
        "basis": dict(Counter(verdicts_basis.get(key_of(r), "unspecified")
                              for r in numeric_pass)),
    }

    # ------------------------------------------------------------ compute acct
    elapsed = [r.get("elapsed_sec") for r in rows if r.get("elapsed_sec")]
    compute = {
        "generations_attempted": total,
        "total_generation_seconds": round(sum(elapsed), 1) if elapsed else None,
        "mean_generation_seconds": round(sum(elapsed) / len(elapsed), 2) if elapsed else None,
        "models": sorted({r["model"] for r in rows}),
        "netlist_bytes_on_disk": sum(
            (out_dir / r["netlist_file"]).stat().st_size
            for r in netlists),
    }

    # ------------------------------------------------------------------ emit
    payload = {
        "run_id": args.run_id,
        "partition": {
            "total": total,
            "excluded_manual_grade_only": len(excluded),
            "no_completion": len(no_completion),
            "netlists": len(netlists),
            "not_converged": len(not_converged),
            "numeric_fail": len(numeric_fail),
            "numeric_pass": len(numeric_pass),
            "converged": len(converged),
        },
        "guard": {
            "rejects_of_netlists": guard_rejects_netlists,
            "netlists": len(netlists),
            "rejects_of_numeric_passes": guard_rejects_passes,
            "numeric_passes": len(numeric_pass),
        },
        "flags": flag_counts,
        "pin_count_mismatch": pin_count,
        "pin_role": {"by_arm": pin_role_by_arm, "passing_total": pin_role_pass_total},
        "arms": per_arm,
        "models": per_model,
        "grading": {
            "numeric_passes": len(numeric_pass),
            "audit_flagged": len(audit_flagged),
            "audit_cleared": len(audit_cleared),
            "cleared_hand_graded": len(graded_cleared),
            "cleared_graded_fake": len(graded_fake),
            "cleared_graded_real": len(graded_real),
            "cleared_measured_only": len(measured_cleared),
            "cleared_unresolved": len(unresolved),
            "human_confirmed_fake": human_confirmed_fake,
            "audit_flagged_assumed_fake": assumed_fake,
            "false_acceptance_assuming_flagged_fake": total_fake_assumed,
            "false_acceptance_rate_ci": [fp_lo, fp_hi],
            "audit_recall_vs_confirmed": None if total_fake_assumed == 0 else
            pct(assumed_fake, total_fake_assumed),
        },
        "confusion": confusion,
        "full_protocol": full,
        "compute": compute,
    }

    if _failures:
        print("INTEGRITY FAILURES:", file=sys.stderr)
        for f in _failures:
            print("  -", f, file=sys.stderr)
        return 1

    (dest / "reconcile.json").write_text(json.dumps(payload, indent=2))

    # ---- numbers.tex : macros the paper uses so no number is hand-copied ----
    p = payload["partition"]
    p_ = p
    b, l = per_arm["bare"], per_arm["library"]
    g = payload["grading"]
    cf = payload["confusion"]
    fu = payload["full_protocol"]
    macros = f"""% GENERATED by bench/reconcile.py -- do not edit by hand.
% Regenerate: python bench/reconcile.py {args.run_id}
\\newcommand{{\\NumAttempted}}{{{p['total']}}}
\\newcommand{{\\NumExcluded}}{{{p['excluded_manual_grade_only']}}}
\\newcommand{{\\NumNoCompletion}}{{{p['no_completion']}}}
\\newcommand{{\\NumNetlists}}{{{p['netlists']}}}
\\newcommand{{\\NumNotConverged}}{{{p['not_converged']}}}
\\newcommand{{\\NumConverged}}{{{p['converged']}}}
\\newcommand{{\\NumNumericFail}}{{{p['numeric_fail']}}}
\\newcommand{{\\NumNumericPass}}{{{p['numeric_pass']}}}
\\newcommand{{\\GuardRejects}}{{{payload['guard']['rejects_of_netlists']}}}
\\newcommand{{\\GuardRejectsPasses}}{{{payload['guard']['rejects_of_numeric_passes']}}}
\\newcommand{{\\FlagSubstituted}}{{{flag_counts['substituted']}}}
\\newcommand{{\\FlagStub}}{{{flag_counts['behavioral_stub']}}}
\\newcommand{{\\FlagNoPart}}{{{flag_counts['no_part']}}}
\\newcommand{{\\FlagPinRole}}{{{flag_counts['pin_role_mismatch']}}}
\\newcommand{{\\PinCountMismatch}}{{{pin_count}}}
\\newcommand{{\\PinRolePassing}}{{{pin_role_pass_total}}}
\\newcommand{{\\PinRoleConverged}}{{{sum(v['converged'] for v in pin_role_by_arm.values())}}}
\\newcommand{{\\GeminiBareNetlists}}{{{per_model[GEMINI]['bare']['netlists']}}}
\\newcommand{{\\GeminiBareLocalSubckt}}{{{per_model[GEMINI]['bare']['local_subckt']}}}
\\newcommand{{\\GeminiBareUnresolvable}}{{{per_model[GEMINI]['bare']['unresolvable']}}}
\\newcommand{{\\PinRoleBarePassing}}{{{pin_role_by_arm['bare']['numeric_pass']}}}
\\newcommand{{\\PinRoleLibPassing}}{{{pin_role_by_arm['library']['numeric_pass']}}}
\\newcommand{{\\BareAttempted}}{{{b['attempted']}}}
\\newcommand{{\\BareNetlists}}{{{b['netlists']}}}
\\newcommand{{\\BarePasses}}{{{b['numeric_pass']}}}
\\newcommand{{\\BareFalse}}{{{b['false_pass']}}}
\\newcommand{{\\BareFalseRate}}{{{b['false_pass_rate']:.0f}}}
\\newcommand{{\\BareGenuine}}{{{b['genuine']}}}
\\newcommand{{\\BareGenuineRate}}{{{b['genuine_rate_over_netlists']:.0f}}}
\\newcommand{{\\LibAttempted}}{{{l['attempted']}}}
\\newcommand{{\\LibNetlists}}{{{l['netlists']}}}
\\newcommand{{\\LibNoCompletion}}{{{l['no_completion']}}}
\\newcommand{{\\LibPasses}}{{{l['numeric_pass']}}}
\\newcommand{{\\LibFalse}}{{{l['false_pass']}}}
\\newcommand{{\\LibFalseRate}}{{{l['false_pass_rate']:.0f}}}
\\newcommand{{\\LibGenuine}}{{{l['genuine']}}}
\\newcommand{{\\LibGenuineRate}}{{{l['genuine_rate_over_netlists']:.0f}}}
\\newcommand{{\\AuditFlagged}}{{{g['audit_flagged']}}}
\\newcommand{{\\AuditCleared}}{{{g['audit_cleared']}}}
\\newcommand{{\\ClearedGraded}}{{{g['cleared_hand_graded']}}}
\\newcommand{{\\ClearedGradedFake}}{{{g['cleared_graded_fake']}}}
\\newcommand{{\\ClearedGradedReal}}{{{g['cleared_graded_real']}}}
\\newcommand{{\\ClearedMeasured}}{{{g['cleared_measured_only']}}}
\\newcommand{{\\ClearedUnresolved}}{{{g['cleared_unresolved']}}}
\\newcommand{{\\FalseAcceptTotal}}{{{cf['labelled_fake']}}}
\\newcommand{{\\FalseAcceptRate}}{{{pct(cf['labelled_fake'], p['numeric_pass']):.0f}}}
\\newcommand{{\\ConfTP}}{{{cf['tp']}}}
\\newcommand{{\\ConfFP}}{{{cf['fp']}}}
\\newcommand{{\\ConfFN}}{{{cf['fn']}}}
\\newcommand{{\\ConfTN}}{{{cf['tn']}}}
\\newcommand{{\\AuditPrecision}}{{{cf['precision']:.0f}}}
\\newcommand{{\\AuditRecall}}{{{cf['recall']:.0f}}}
\\newcommand{{\\AuditSpecificity}}{{{cf['specificity']:.0f}}}
\\newcommand{{\\AuditFOne}}{{{cf['f1']:.0f}}}
\\newcommand{{\\AuditAccuracy}}{{{cf['accuracy']:.0f}}}
\\newcommand{{\\WaveEvaluated}}{{{fu['wave_evaluated']}}}
\\newcommand{{\\LabelledReal}}{{{cf['labelled_real']}}}
\\newcommand{{\\FalseAcceptCILo}}{{{fp_lo:.0f}}}
\\newcommand{{\\FalseAcceptCIHi}}{{{fp_hi:.0f}}}
\\newcommand{{\\BareFalseCILo}}{{{b['false_pass_ci'][0]:.0f}}}
\\newcommand{{\\BareFalseCIHi}}{{{b['false_pass_ci'][1]:.0f}}}
\\newcommand{{\\LibFalseCILo}}{{{l['false_pass_ci'][0]:.0f}}}
\\newcommand{{\\LibFalseCIHi}}{{{l['false_pass_ci'][1]:.0f}}}
\\newcommand{{\\BareValidPass}}{{{b['valid_pass']}}}
\\newcommand{{\\LibValidPass}}{{{l['valid_pass']}}}
\\newcommand{{\\BareValidRate}}{{{b['valid_pass_per_attempted']:.1f}}}
\\newcommand{{\\LibValidRate}}{{{l['valid_pass_per_attempted']:.1f}}}
\\newcommand{{\\BareGenuineRateAtt}}{{{b['genuine_rate_over_attempted']:.0f}}}
\\newcommand{{\\LibGenuineRateAtt}}{{{l['genuine_rate_over_attempted']:.0f}}}
"""
    (dest / "numbers.tex").write_text(macros)

    # ------------------------------------------------------------------------
    # FACTORIAL (AR-5): requirement x availability, all 4 arms, all rows.
    #
    # Deliberately NOT scoped to bare/library like everything above -- this is
    # the one place `requirement` rows are allowed to be read. Emits
    # into a separate file/macro namespace so it can never blend into the
    # existing bare/library-only numbers by accident (see the comment on the
    # `rows` filter near the top of main()).
    #
    # "Reference attempted" is a STRUCTURAL signal (has_library and not
    # substituted), computed over every netlist that was produced, whether or
    # not it went on to simulate. It is not the same claim as "successfully
    # used the real part" -- a netlist can reference an external library and
    # still fail to simulate because the referenced file doesn't exist under
    # that name. Both figures are reported, explicitly labelled, for exactly
    # this reason: collapsing them would repeat the error this file exists to
    # prevent.
    # ------------------------------------------------------------------------
    # The reported design is the three-arm ablation: a common baseline and the
    # two factors varied one at a time. Any other prompt variant present in
    # results.jsonl is out of scope for this paper and is filtered out here so
    # it can never leak into a denominator.
    FULL_ARMS = ("bare", "requirement", "library")
    REQUIRES = {"bare": False, "requirement": True, "library": False}
    AVAILABLE = {"bare": False, "requirement": False, "library": True}
    all_rows = [r for r in load(args.run_id)[1]
                if r.get("prompt_variant", "bare") in FULL_ARMS]
    fact_rows = [r for r in all_rows if not r.get("manual_grade_only")]

    def fhas_netlist(r):
        nf = r.get("netlist_file")
        return bool(nf) and (out_dir / nf).exists()


    fact_ids = [(r.get("case_id"), r.get("model"), r.get("sample"), arm(r))
                for r in fact_rows]
    check(len(fact_ids) == len(set(fact_ids)),
          "factorial: duplicate generation ids across arms")

    fact_arms = {}
    fact_models = {}
    for a in FULL_ARMS:
        a_rows = [r for r in fact_rows if arm(r) == a]
        a_net = [r for r in a_rows if fhas_netlist(r)]
        a_nocomp = [r for r in a_rows if not fhas_netlist(r)]
        check(all(r.get("status") == "LLM_ERROR" for r in a_nocomp),
              f"factorial {a}: a no-netlist row is not LLM_ERROR")
        a_conv = [r for r in a_net if r.get("converged")]
        a_pass = [r for r in a_conv if r.get("sim_pass")]
        # Audit-flagged, not graded: only bare/library passes carry manual
        # grades, so a grade-based count here would read 0 for `requirement`.
        a_false = [r for r in a_pass if not r.get("audited_pass")]
        a_ref = [r for r in a_net if is_genuine(r)]
        # The stronger predicate: the reference actually resolves to a vendor
        # model. The distance between a_ref and a_res is the paper's own
        # argument applied one level down -- writing a `.lib` line is not the
        # same act as naming a file that exists.
        a_res = [r for r in a_net if is_resolved_vendor(r, out_dir)]
        ref_lo, ref_hi = clustered_bootstrap_ci(a_net, is_genuine, lambda r: True)
        res_lo, res_hi = clustered_bootstrap_ci(
            a_net, lambda r: is_resolved_vendor(r, out_dir), lambda r: True)
        fact_arms[a] = {
            "requires": REQUIRES[a], "available": AVAILABLE[a],
            "attempted": len(a_rows), "no_completion": len(a_nocomp),
            "netlists": len(a_net), "converged": len(a_conv),
            "numeric_pass": len(a_pass), "false_pass": len(a_false),
            "reference_attempted": len(a_ref),
            "reference_rate": pct(len(a_ref), len(a_net)) if a_net else None,
            "reference_ci": ref_lo, "reference_ci_hi": ref_hi,
            "resolved_vendor": len(a_res),
            "resolved_rate": pct(len(a_res), len(a_net)) if a_net else None,
            "resolved_rate_itt": pct(len(a_res), len(a_rows)) if a_rows else None,
            "resolved_ci": res_lo, "resolved_ci_hi": res_hi,
            "models_present": sorted({r["model"] for r in a_rows}),
        }
        for m in sorted({r["model"] for r in a_rows}):
            m_rows = [r for r in a_rows if r["model"] == m]
            m_net = [r for r in m_rows if fhas_netlist(r)]
            m_ref = [r for r in m_net if is_genuine(r)]
            fact_models.setdefault(m, {})[a] = {
                "attempted": len(m_rows),
                "no_completion": len(m_rows) - len(m_net),
                "netlists": len(m_net),
                "reference_attempted": len(m_ref),
                "reference_rate": pct(len(m_ref), len(m_net)) if m_net else None,
            }

    all_models = sorted(fact_models)
    check(all(len(fact_arms[a]["models_present"]) == len(all_models)
              for a in FULL_ARMS),
          "factorial: an arm is missing a model")

    if _failures:
        print("INTEGRITY FAILURES:", file=sys.stderr)
        for f in _failures:
            print("  -", f, file=sys.stderr)
        return 1

    fact_payload = {"arms": fact_arms, "models": fact_models}
    (dest / "factorial.json").write_text(json.dumps(fact_payload, indent=2))

    def farm(a):
        return fact_arms[a]

    fmacros_lines = ["% GENERATED by bench/reconcile.py -- do not edit by hand.",
                      f"% Regenerate: python bench/reconcile.py {args.run_id}"]
    for a in FULL_ARMS:
        tag = a.capitalize()
        v = farm(a)
        for key, mname in [("attempted", "Att"), ("no_completion", "NoComp"),
                            ("netlists", "Net"), ("converged", "Conv"),
                            ("numeric_pass", "Pass"), ("false_pass", "False"),
                            ("reference_attempted", "Ref"),
                            ("resolved_vendor", "Res")]:
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{tag}{mname}}}{{{v[key]}}}")
        rr = v["reference_rate"]
        fmacros_lines.append(
            f"\\newcommand{{\\Fact{tag}RefRate}}{{{rr:.0f}}}" if rr is not None
            else f"\\newcommand{{\\Fact{tag}RefRate}}{{n/a}}")
        for key, mname in [("resolved_rate", "ResRate"),
                            ("resolved_rate_itt", "ResRateItt"),
                            ("resolved_ci", "ResCILo"),
                            ("resolved_ci_hi", "ResCIHi")]:
            val = v[key]
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{tag}{mname}}}{{{val:.0f}}}"
                if val is not None else
                f"\\newcommand{{\\Fact{tag}{mname}}}{{n/a}}")
        fmacros_lines.append(
            f"\\newcommand{{\\Fact{tag}RefCILo}}{{{v['reference_ci']:.0f}}}")
        fmacros_lines.append(
            f"\\newcommand{{\\Fact{tag}RefCIHi}}{{{v['reference_ci_hi']:.0f}}}")

    MODEL_TAG = {"anthropic/claude-opus-5": "Claude",
                 "google/gemini-3.1-pro-preview": "Gemini",
                 "openai/gpt-5.6-sol": "Gpt"}
    for m, mtag in MODEL_TAG.items():
        for a in FULL_ARMS:
            v = fact_models.get(m, {}).get(a)
            atag = a.capitalize()
            if v is None:
                continue
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{atag}{mtag}Att}}{{{v['attempted']}}}")
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{atag}{mtag}NoComp}}{{{v['no_completion']}}}")
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{atag}{mtag}Net}}{{{v['netlists']}}}")
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{atag}{mtag}Ref}}{{{v['reference_attempted']}}}")
            rr = v["reference_rate"]
            fmacros_lines.append(
                f"\\newcommand{{\\Fact{atag}{mtag}RefRate}}{{{rr:.0f}}}" if rr is not None
                else f"\\newcommand{{\\Fact{atag}{mtag}RefRate}}{{n/a}}")
    (dest / "factorial.tex").write_text("\n".join(fmacros_lines) + "\n")

    ftbl = []
    header = (r"""% GENERATED by bench/reconcile.py -- do not edit by hand.
\begin{table}[t]
\centering
\caption{Requirement $\times$ availability, each factor varied alone against a
  common baseline. \emph{Ref.} is netlists that structurally reference an
  external library rather than defining a local subcircuit --- a claim about
  what was \emph{written}. \emph{Res.} is the subset whose reference actually
  \emph{resolves} to a vendor-supplied definition of the part on the target
  machine, with the resolved file's SHA-256 recorded. \emph{Pass} is the
  numeric gate. Rates are over netlists produced; the bracketed rate is
  intention-to-treat over all attempted generations, counting a filtered
  completion as a failure.}
\label{tab:factorial}
\small
\begin{tabular}{@{}lccccccc@{}}
\toprule
\textbf{Arm} & \textbf{Require} & \textbf{Avail.} & \textbf{Att.} &
  \textbf{Net.} & \textbf{Ref./Net.} & \textbf{Res./Net.} & \textbf{Pass/Net.} \\
\midrule
""")
    rows_tex = []
    for a in FULL_ARMS:
        v = farm(a)
        req = "yes" if v["requires"] else "no"
        avail = "yes" if v["available"] else "no"
        ref_str = (rf"{v['reference_attempted']}/{v['netlists']} "
                   rf"({v['reference_rate']:.0f}\%)") if v["netlists"] else "n/a"
        res_str = (rf"{v['resolved_vendor']}/{v['netlists']} "
                   rf"({v['resolved_rate']:.0f}\%) [{v['resolved_rate_itt']:.0f}\%]") \
            if v["netlists"] else "n/a"
        rows_tex.append(
            rf"\textsc{{{a}}} & {req} & {avail} & {v['attempted']} & {v['netlists']} & "
            rf"{ref_str} & {res_str} & {v['numeric_pass']}/{v['netlists']} \\")
    ftbl.append(header + "\n".join(rows_tex) + "\n\\bottomrule\n\\end{tabular}\n\\end{table}\n")
    (dest / "factorial_tables.tex").write_text("\n".join(ftbl))

    print()
    print("=" * 72)
    print("FACTORIAL (AR-5)  requirement x availability")
    print("=" * 72)
    for a in FULL_ARMS:
        v = farm(a)
        print(f"  {a:12s} req={str(v['requires']):5s} avail={str(v['available']):5s} "
              f"attempted={v['attempted']:3d} lost={v['no_completion']:3d} "
              f"netlists={v['netlists']:3d} converged={v['converged']:3d} "
              f"pass={v['numeric_pass']:3d} "
              f"ref={v['reference_attempted']:3d}/{v['netlists']:3d} "
              f"({v['reference_rate']:.0f}% CI [{v['reference_ci']:.0f},"
              f"{v['reference_ci_hi']:.0f}])"
              if v["netlists"] else
              f"  {a:12s} req={str(v['requires']):5s} avail={str(v['available']):5s} "
              f"attempted={v['attempted']:3d} lost={v['no_completion']:3d} netlists=0")
        for m in v["models_present"]:
            mv = fact_models[m][a]
            rr = f"{mv['reference_rate']:.0f}%" if mv["reference_rate"] is not None else "n/a"
            print(f"      {m.split('/')[-1]:24s} att={mv['attempted']:3d} "
                  f"lost={mv['no_completion']:3d} net={mv['netlists']:3d} "
                  f"ref={mv['reference_attempted']:3d}/{mv['netlists']:3d} ({rr})")
    print()
    print(f"  wrote {dest/'factorial.tex'}")
    print(f"  wrote {dest/'factorial_tables.tex'}")
    print(f"  wrote {dest/'factorial.json'}")

    # ---- tables.tex --------------------------------------------------------
    def fill(template: str, mapping: dict) -> str:
        for k, v in mapping.items():
            template = template.replace("@" + k + "@", str(v))
        leftover = re.findall(r"@[A-Z_]+@", template)
        if leftover:
            raise IntegrityError(f"unfilled table tokens: {sorted(set(leftover))}")
        return template

    tbl = []
    tbl.append(fill(r"""% GENERATED by bench/reconcile.py -- do not edit by hand.
\begin{table}[t]
\centering
\caption{Disposition of all @TOTAL@ generations. Categories are mutually
  exclusive and sum to the total, so every generation is accounted for.}
\label{tab:funnel}
\small
\begin{tabular}{@{}lcl@{}}
\toprule
\textbf{Category} & \textbf{$n$} & \textbf{Meaning} \\
\midrule
Held out for manual grading & @EXCL@ & excluded from all rates below \\
No completion returned      & @NOCOMP@ & provider-side filtering, no netlist \\
\midrule
\textbf{Netlists produced}  & \textbf{@NETS@} & \textbf{denominator for audit rates} \\
\quad did not converge      & @NOCONV@ & simulator rejected the netlist \\
\quad converged, missed target & @NFAIL@ & ran, outside tolerance \\
\quad \textbf{converged, met target} & \textbf{@NPASS@} & \textbf{denominator for acceptance} \\
\bottomrule
\end{tabular}
\end{table}
""", {"TOTAL": p_["total"], "EXCL": p_["excluded_manual_grade_only"],
      "NOCOMP": p_["no_completion"], "NETS": p_["netlists"],
      "NOCONV": p_["not_converged"], "NFAIL": p_["numeric_fail"],
      "NPASS": p_["numeric_pass"]}))

    tbl.append(fill(r"""
\begin{table}[t]
\centering
\caption{Effect of library context, with explicit denominators. Intervals are
  95\% clustered bootstrap over cases.}
\label{tab:main}
\small
\begin{tabular}{@{}lcc@{}}
\toprule
 & \textbf{\textsc{bare}} & \textbf{\textsc{library}} \\
\midrule
Generations attempted         & @BATT@ & @LATT@ \\
Netlists produced             & @BNET@ & @LNET@ \\
Vendor-library reference      & @BGEN@ & @LGEN@ \\
\quad 95\% CI                 & [@BGLO@, @BGHI@]\% & [@LGLO@, @LGHI@]\% \\
Numeric passes                & @BPASS@ & @LPASS@ \\
False acceptance among passes & @BFALSE@ & @LFALSE@ \\
\quad 95\% CI                 & [@BFLO@, @BFHI@]\% & [@LFLO@, @LFHI@]\% \\
\midrule
\multicolumn{3}{@{}l}{\emph{Unconditional, over all attempted generations}} \\
Numeric passes / attempted    & @BPA@ & @LPA@ \\
False passes / attempted      & @BFA@ & @LFA@ \\
\bottomrule
\end{tabular}
\end{table}
""", {"BATT": b["attempted"], "LATT": l["attempted"],
      "BNET": b["netlists"], "LNET": l["netlists"],
      "BGEN": frac(b["genuine"], b["netlists"]),
      "LGEN": frac(l["genuine"], l["netlists"]),
      "BGLO": f"{b['genuine_ci'][0]:.0f}", "BGHI": f"{b['genuine_ci'][1]:.0f}",
      "LGLO": f"{l['genuine_ci'][0]:.0f}", "LGHI": f"{l['genuine_ci'][1]:.0f}",
      "BPASS": frac(b["numeric_pass"], b["netlists"]),
      "LPASS": frac(l["numeric_pass"], l["netlists"]),
      "BFALSE": frac(b["false_pass"], b["numeric_pass"]),
      "LFALSE": frac(l["false_pass"], l["numeric_pass"]),
      "BFLO": f"{b['false_pass_ci'][0]:.0f}", "BFHI": f"{b['false_pass_ci'][1]:.0f}",
      "LFLO": f"{l['false_pass_ci'][0]:.0f}", "LFHI": f"{l['false_pass_ci'][1]:.0f}",
      "BPA": frac(b["numeric_pass"], b["attempted"]),
      "LPA": frac(l["numeric_pass"], l["attempted"]),
      "BFA": frac(b["false_pass"], b["attempted"]),
      "LFA": frac(l["false_pass"], l["attempted"])}))

    tbl.append(fill(r"""
\begin{table}[t]
\centering
\caption{Pin-role mismatches by arm. Only @PRPASS@ of @PRTOT@ reach the numeric
  gate, and @PRBARE@ of those @PRPASS@ are in the \textsc{bare} arm.}
\label{tab:pinrole}
\small
\begin{tabular}{@{}lccc@{}}
\toprule
\textbf{Arm} & \textbf{Flagged} & \textbf{Converged} & \textbf{Numeric pass} \\
\midrule
\textsc{bare}    & @BT@ & @BC@ & @BP@ \\
\textsc{library} & @LT@ & @LC@ & @LP@ \\
\midrule
\textbf{Total}   & \textbf{@PRTOT@} & \textbf{@PRCONV@} & \textbf{@PRPASS@} \\
\bottomrule
\end{tabular}
\end{table}
""", {"PRPASS": pin_role_pass_total, "PRTOT": flag_counts["pin_role_mismatch"],
      "PRBARE": pin_role_by_arm["bare"]["numeric_pass"],
      "BT": pin_role_by_arm["bare"]["total"],
      "BC": pin_role_by_arm["bare"]["converged"],
      "BP": pin_role_by_arm["bare"]["numeric_pass"],
      "LT": pin_role_by_arm["library"]["total"],
      "LC": pin_role_by_arm["library"]["converged"],
      "LP": pin_role_by_arm["library"]["numeric_pass"],
      "PRCONV": sum(v["converged"] for v in pin_role_by_arm.values())}))

    (dest / "tables.tex").write_text("\n".join(tbl))

    # ------------------------------------------------------------------ stdout
    print("=" * 72)
    print(f"RECONCILIATION  run {args.run_id}")
    print("=" * 72)
    print(f"  attempted                        {p['total']}")
    print(f"    held out for manual grading    {p['excluded_manual_grade_only']}")
    print(f"    no completion (LLM_ERROR)      {p['no_completion']}")
    print(f"    netlists produced              {p['netlists']}  <-- audit denominator")
    print(f"      did not converge             {p['not_converged']}")
    print(f"      converged, missed target     {p['numeric_fail']}")
    print(f"      converged, met target        {p['numeric_pass']}  <-- acceptance denominator")
    print()
    print("  GUARD")
    print(f"    rejects {guard_rejects_netlists}/{len(netlists)} netlists "
          f"= {pct(guard_rejects_netlists, len(netlists)):.1f}%")
    print(f"    rejects {guard_rejects_passes}/{len(numeric_pass)} numeric passes "
          f"= {pct(guard_rejects_passes, len(numeric_pass)):.1f}%")
    if guard_rejects_netlists == len(netlists):
        print("    !! rejects every candidate -> no discriminative power in this setting")
    print()
    print("  DETECTOR FLAGS (over %d netlists)" % len(netlists))
    for f, n in flag_counts.items():
        print(f"    {f:20s} {n:3d}  ({pct(n, len(netlists)):.0f}%)")
    print(f"    {'pin_count_mismatch':20s} {pin_count:3d}  (simulator-caught)")
    print()
    print("  PIN-ROLE by arm (flagged / converged / numeric pass)")
    for a, v in pin_role_by_arm.items():
        print(f"    {a:8s} {v['total']:3d} / {v['converged']:3d} / {v['numeric_pass']:3d}")
    print()
    print("  ARMS")
    for a, v in per_arm.items():
        print(f"    {a:8s} attempted={v['attempted']:3d} netlists={v['netlists']:3d} "
              f"(lost {v['no_completion']:2d})  genuine={v['genuine']:3d} "
              f"({v['genuine_rate_over_netlists']:.0f}% of netlists)  "
              f"pass={v['numeric_pass']:3d}  false={v['false_pass']:3d} "
              f"({v['false_pass_rate']:.0f}%, CI [{v['false_pass_ci'][0]:.0f},"
              f"{v['false_pass_ci'][1]:.0f}])")
    print()
    print("  GRADING")
    print(f"    numeric passes                 {g['numeric_passes']}")
    print(f"      audit flagged (ASSUMED fake) {g['audit_flagged']}")
    print(f"      audit cleared                {g['audit_cleared']}")
    print(f"        hand graded                {g['cleared_hand_graded']}"
          f"  (fake {g['cleared_graded_fake']}, real {g['cleared_graded_real']})")
    print(f"        measurement only           {g['cleared_measured_only']}")
    print(f"        unresolved                 {g['cleared_unresolved']}")
    print()
    print("  CONFUSION MATRIX (audit vs independent labels, all "
          f"{p['numeric_pass']} passes)")
    print("                    labelled FAKE   labelled REAL")
    print(f"    audit flags        TP={cf['tp']:4d}          FP={cf['fp']:4d}")
    print(f"    audit clears       FN={cf['fn']:4d}          TN={cf['tn']:4d}")
    print(f"    precision={cf['precision']:.1f}%  recall={cf['recall']:.1f}%  "
          f"specificity={cf['specificity']:.1f}%  F1={cf['f1']:.1f}%  "
          f"acc={cf['accuracy']:.1f}%")
    print(f"    label basis: {cf['basis']}")
    print(f"    FALSE ACCEPTANCE {cf['labelled_fake']}/{p['numeric_pass']} "
          f"= {pct(cf['labelled_fake'], p['numeric_pass']):.0f}% "
          f"CI [{fp_lo:.0f},{fp_hi:.0f}]")
    print()
    print("  COMPUTE")
    for k, v in compute.items():
        print(f"    {k:28s} {v}")
    print()

    # ========================================================================
    # CORPUS ACCOUNTING, ACCEPTANCE LADDER, AND Pass@k
    #
    # Three things a reader should be able to check without trusting prose.
    #
    # (a) Corpus accounting across all three arms in one place. The paper
    #     previously quoted a 540-generation corpus alongside per-arm figures
    #     that had already had the inspection-only case removed, so the arm
    #     counts summed to 531 and the reader was left to reconcile them.
    #     Every denominator is emitted here from one partition.
    # (b) What each acceptance check contributes, as a ladder and as a
    #     leave-one-out, so an aggregate precision/recall cannot hide the fact
    #     that one predicate is doing the work.
    # (c) Pass@1 / Pass@3 under numeric, provenance and full-protocol
    #     acceptance -- the paper argues numeric Pass@k overstates what was
    #     verified, and this is that claim as a measurement.
    # ========================================================================
    corpus_rows = all_rows                      # already scoped to FULL_ARMS
    corpus_scored = [r for r in corpus_rows if not r.get("manual_grade_only")]
    corpus_excluded = [r for r in corpus_rows if r.get("manual_grade_only")]
    per_arm_attempted = {a: sum(1 for r in corpus_rows if arm(r) == a)
                         for a in FULL_ARMS}
    per_arm_scored = {a: sum(1 for r in corpus_scored if arm(r) == a)
                      for a in FULL_ARMS}
    check(len(corpus_rows) == sum(per_arm_attempted.values()),
          "corpus total does not equal the sum of per-arm attempts")
    check(len(set(per_arm_attempted.values())) == 1,
          "arms have unequal attempt counts; the design is balanced by "
          "construction so this is a corpus error, not a finding")
    check(len(corpus_scored) + len(corpus_excluded) == len(corpus_rows),
          "scored and excluded generations do not partition the corpus")

    corpus = {
        "total": len(corpus_rows),
        "excluded_inspection": len(corpus_excluded),
        "scored": len(corpus_scored),
        "arms": len(FULL_ARMS),
        "per_arm_attempted": next(iter(set(per_arm_attempted.values()))),
        "per_arm_scored": next(iter(set(per_arm_scored.values()))),
        "cases": len({r["case_id"] for r in corpus_rows}),
        "models": len({r["model"] for r in corpus_rows}),
        "samples_per_cell": (len(corpus_rows)
                             // (len({r["case_id"] for r in corpus_rows})
                                 * len({r["model"] for r in corpus_rows})
                                 * len(FULL_ARMS))),
    }

    # ---------------------------------------------------------------- ladder
    # Predicates are needed for every numeric pass in every arm, not only the
    # bare/library passes the grading covers: Pass@k is reported for all three
    # arms, and a missing key there would be a crash rather than a zero.
    pred_by_key = {}
    for r in corpus_scored:
        if not r.get("sim_pass"):
            continue
        text = (out_dir / r["netlist_file"]).read_text(
            encoding="utf-8", errors="ignore")
        pred_by_key[key_of(r)] = ablation.predicate_values(r, text)
    check(all(key_of(r) in pred_by_key for r in numeric_pass),
          "a graded numeric pass has no re-derived predicates")

    # Grounds for each grade, partitioned from the stored verdicts rather than
    # typed into the table: the previous hand-written version reused a macro
    # counting audit-cleared valid grades (20) where the row needed valid
    # grades settled by reading (11), so the table summed to 109 rather than
    # 100. Deriving it makes that class of error a build failure.
    grounds = Counter()
    for r in numeric_pass:
        v = verdicts_full.get(key_of(r), {})
        note = (v.get("note") or "").lower()
        if v.get("verdict") == "FAKE":
            if "subckt" in note:
                grounds["fake_local_subckt"] += 1
            elif "never instantiated" in note:
                grounds["fake_no_instance"] += 1
            else:
                grounds["fake_read"] += 1
        else:
            if v.get("basis") == "distortion measurement":
                grounds["valid_distortion"] += 1
            elif v.get("basis") == "mechanical re-verification":
                grounds["valid_reverified"] += 1
            else:
                grounds["valid_read"] += 1
    check(sum(grounds.values()) == len(numeric_pass),
          "grade grounds do not partition the numeric passes")
    check(grounds["fake_local_subckt"] + grounds["fake_no_instance"]
          + grounds["fake_read"] == cf["labelled_fake"],
          "grade grounds disagree with the false-acceptance total")
    check(grounds["valid_distortion"] + grounds["valid_read"]
          + grounds["valid_reverified"] == cf["labelled_real"],
          "grade grounds disagree with the valid total")

    # A grade must not contradict the mechanical re-verification. Nine
    # judgement-dependent grades once did: they were graded false on a
    # worksheet THD figure that did not reproduce, while the part was
    # necessary and the output undistorted. Making disagreement a build
    # failure is what keeps that class of error from recurring silently.
    rev_path = out_dir / "reverification.jsonl"
    reverify = {}
    if rev_path.exists():
        for line in rev_path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                reverify[(d["case_id"], d["model"], d["sample"],
                          d["prompt_variant"])] = d
    contradictions = []
    for r in numeric_pass:
        d = reverify.get(key_of(r))
        if not d or d.get("control") != "met":
            continue
        mechanically_valid = d.get("part_necessary") and not d.get("distorted")
        if (verdicts.get(key_of(r)) == "FAKE") == bool(mechanically_valid):
            contradictions.append(key_of(r))
    check(not contradictions,
          f"{len(contradictions)} grade(s) contradict reverification.jsonl: "
          f"{contradictions[:3]}")
    rev_rows = [d for d in reverify.values() if d.get("control") == "met"]
    reverification = {
        "qualified_checked": len(rev_rows),
        "part_necessary": sum(1 for d in rev_rows if d.get("part_necessary")),
        "gain_checked": sum(1 for d in rev_rows if d.get("thd_fft") is not None),
        "distorted": sum(1 for d in rev_rows if d.get("distorted")),
        "response_checked": sum(1 for d in rev_rows if d.get("response")),
        "responds": sum(1 for d in rev_rows
                        if (d.get("response") or {}).get("responds")),
        "max_thd_fft": max((d["thd_fft"] for d in rev_rows
                            if d.get("thd_fft") is not None), default=0.0),
    }

    stages = ablation.stage_matrix(numeric_pass, pred_by_key, key_of,
                                   _is_fake, _wave_rejects)
    check(stages[0]["accepted"] == len(numeric_pass),
          "stage matrix does not start from the numeric passes")
    check(all(st["accepted"] > 0 for st in stages),
          "a protocol stage accepts nothing; its composition shares would "
          "divide by zero")
    check(all(st["false_accepted"] + st["valid_accepted"] == st["accepted"]
              for st in stages),
          "a protocol stage's false and valid counts do not sum to accepted")

    rungs = ablation.ladder(numeric_pass, pred_by_key, key_of, _is_fake)
    solo = ablation.standalone(numeric_pass, pred_by_key, key_of, _is_fake)
    loo = ablation.leave_one_out(numeric_pass, pred_by_key, key_of, _is_fake)
    minimal = ablation.minimal_sufficient(numeric_pass, pred_by_key, key_of, _is_fake)
    prov_only = ablation.score(numeric_pass, pred_by_key, key_of, _is_fake,
                               ("resolved_binding",))
    check(prov_only["tp"] + prov_only["fp"] + prov_only["fn"]
          + prov_only["tn"] == len(numeric_pass),
          "provenance-only matrix does not partition the numeric passes")

    # ---------------------------------------------------------------- Pass@k
    # Intention-to-treat: every (case, model, arm) cell has exactly k
    # attempts, and a filtered completion is a failed attempt.
    def _numeric(r):
        return bool(r.get("sim_pass"))

    def _prov(r):
        return _numeric(r) and not pred_by_key[key_of(r)]["resolved_binding"]

    def _full(r):
        if not _prov(r):
            return False
        if pred_by_key[key_of(r)]["pin_role_mismatch"]:
            return False
        return not _wave_rejects(r)

    passk = ablation.pass_at_k(
        corpus_scored, key_of,
        {"numeric": _numeric, "provenance": _prov, "full_protocol": _full},
        k=corpus["samples_per_cell"])

    # Every check above this point -- corpus accounting, grade grounds, the
    # stage matrix, and agreement with the mechanical re-verification -- must
    # be enforced before anything derived from it is written. These checks
    # were once appended after the last enforcement point, where a failure was
    # recorded but never acted on and the run still reported success.
    if _failures:
        print("INTEGRITY FAILURES:", file=sys.stderr)
        for f in _failures:
            print("  -", f, file=sys.stderr)
        return 1

    payload["corpus"] = corpus
    payload["ladder"] = rungs
    payload["grounds"] = dict(grounds)
    payload["stages"] = stages
    payload["standalone"] = solo
    payload["leave_one_out"] = {k: v for k, v in loo.items() if k != "_full"}
    payload["minimal_sufficient"] = minimal
    payload["provenance_only"] = prov_only
    payload["pass_at_k"] = passk
    (dest / "reconcile.json").write_text(json.dumps(payload, indent=2))

    # ------------------------------------------------------------ new macros
    kk = corpus["samples_per_cell"]
    extra = ["% GENERATED by bench/reconcile.py -- do not edit by hand.",
             f"% Regenerate: python bench/reconcile.py {args.run_id}",
             rf"\newcommand{{\CorpusTotal}}{{{corpus['total']}}}",
             rf"\newcommand{{\CorpusExcluded}}{{{corpus['excluded_inspection']}}}",
             rf"\newcommand{{\CorpusScored}}{{{corpus['scored']}}}",
             rf"\newcommand{{\CorpusArms}}{{{corpus['arms']}}}",
             rf"\newcommand{{\ArmAttempted}}{{{corpus['per_arm_attempted']}}}",
             rf"\newcommand{{\ArmScored}}{{{corpus['per_arm_scored']}}}",
             rf"\newcommand{{\NumCases}}{{{corpus['cases']}}}",
             rf"\newcommand{{\NumModels}}{{{corpus['models']}}}",
             rf"\newcommand{{\SamplesPerCell}}{{{kk}}}",
             rf"\newcommand{{\ProvTP}}{{{prov_only['tp']}}}",
             rf"\newcommand{{\ProvFP}}{{{prov_only['fp']}}}",
             rf"\newcommand{{\ProvFN}}{{{prov_only['fn']}}}",
             rf"\newcommand{{\ProvTN}}{{{prov_only['tn']}}}",
             rf"\newcommand{{\ProvPrecision}}{{{prov_only['precision']:.0f}}}",
             rf"\newcommand{{\ProvRecall}}{{{prov_only['recall']:.0f}}}",
             rf"\newcommand{{\MinimalSubsetSize}}{{{len(minimal['subset'])}}}",
             rf"\newcommand{{\MinimalSubsetFP}}{{{minimal['fp']}}}",
             rf"\newcommand{{\GroundsFakeSubckt}}{{{grounds['fake_local_subckt']}}}",
             rf"\newcommand{{\GroundsFakeNoInstance}}{{{grounds['fake_no_instance']}}}",
             rf"\newcommand{{\GroundsFakeRead}}{{{grounds['fake_read']}}}",
             rf"\newcommand{{\GroundsValidDistortion}}{{{grounds['valid_distortion']}}}",
             rf"\newcommand{{\GroundsValidRead}}{{{grounds['valid_read']}}}",
             rf"\newcommand{{\GroundsValidReverified}}{{{grounds['valid_reverified']}}}",
             rf"\newcommand{{\RevChecked}}{{{reverification['qualified_checked']}}}",
             rf"\newcommand{{\RevNecessary}}{{{reverification['part_necessary']}}}",
             rf"\newcommand{{\RevGainChecked}}{{{reverification['gain_checked']}}}",
             rf"\newcommand{{\RevDistorted}}{{{reverification['distorted']}}}",
             rf"\newcommand{{\RevResponseChecked}}{{{reverification['response_checked']}}}",
             rf"\newcommand{{\RevResponds}}{{{reverification['responds']}}}",
             rf"\newcommand{{\RevMaxTHD}}{{{reverification['max_thd_fft']:.3f}}}",
             rf"\newcommand{{\StageProvAccepted}}{{{stages[1]['accepted']}}}",
             rf"\newcommand{{\StageIfaceAccepted}}{{{stages[2]['accepted']}}}",
             rf"\newcommand{{\StageWaveAccepted}}{{{stages[3]['accepted']}}}",
             rf"\newcommand{{\StageWaveRemoved}}{{{stages[3]['removed']}}}",
             rf"\newcommand{{\WaveViolations}}{{{fu['wave_violations']}}}",
             ]
    # Composition of the accepted set at each stage. Hand-setting these in the
    # .tex would put numbers in the paper that the corpus does not police,
    # which is the failure mode this whole step exists to prevent.
    STAGE_TAG = ["Numeric", "Prov", "Iface", "Wave"]
    for tag, st in zip(STAGE_TAG, stages):
        extra.append(rf"\newcommand{{\Stage{tag}FalseShare}}{{"
                     rf"{100.0 * st['false_accepted'] / st['accepted']:.0f}}}")
        extra.append(rf"\newcommand{{\Stage{tag}ValidShare}}{{"
                     rf"{100.0 * st['valid_accepted'] / st['accepted']:.0f}}}")

    for pred, sc in solo.items():
        tag = "".join(w.capitalize() for w in pred.split("_"))
        # Flagged is TP+FP. Reporting TP under a "Flagged" heading made the
        # pin-role row read as 11 flagged / 11 TP / 1 FP, which cannot hold.
        extra.append(rf"\newcommand{{\Solo{tag}Flagged}}{{{sc['flagged']}}}")
        extra.append(rf"\newcommand{{\Solo{tag}Precision}}{{{sc['precision']:.0f}}}")
        extra.append(rf"\newcommand{{\Solo{tag}TP}}{{{sc['tp']}}}")
        extra.append(rf"\newcommand{{\Solo{tag}FP}}{{{sc['fp']}}}")
        extra.append(rf"\newcommand{{\Solo{tag}Recall}}{{{sc['recall']:.0f}}}")
        extra.append(rf"\newcommand{{\Unique{tag}}}{{{loo[pred]['unique_tp']}}}")
    # The cumulative rungs built up from the instance check. These were once
    # typed into the paper by hand and went stale when grades changed.
    for sc in rungs:
        act = sc["active"]
        if not act or act[0] != "no_part" or len(act) == len(ablation.PREDICATES):
            continue
        tag = "".join(w.capitalize() for w in act[-1].split("_"))
        check(sc["precision"] is not None,
              f"ladder rung ending in {act[-1]} flags nothing")
        extra.append(rf"\newcommand{{\Ladder{tag}Flagged}}{{{sc['flagged']}}}")
        extra.append(rf"\newcommand{{\Ladder{tag}TP}}{{{sc['tp']}}}")
        extra.append(rf"\newcommand{{\Ladder{tag}FP}}{{{sc['fp']}}}")
        extra.append(rf"\newcommand{{\Ladder{tag}Precision}}{{{sc['precision']:.0f}}}")
        extra.append(rf"\newcommand{{\Ladder{tag}Recall}}{{{sc['recall']:.0f}}}")
    ARM_TAG ={"bare": "Bare", "library": "Lib", "requirement": "Req"}
    for a, atag in ARM_TAG.items():
        for name, ntag in (("numeric", "Numeric"), ("provenance", "Prov"),
                           ("full_protocol", "Full")):
            v = passk["by_arm"][a][name]
            extra.append(
                rf"\newcommand{{\PassOne{atag}{ntag}}}{{{v['pass_at_1']:.1f}}}")
            extra.append(
                rf"\newcommand{{\PassK{atag}{ntag}}}{{{v[f'pass_at_{kk}']:.1f}}}")
            lo, hi = v["ci_pass_at_1"]
            extra.append(
                rf"\newcommand{{\PassOne{atag}{ntag}CI}}{{[{lo:.1f},{hi:.1f}]}}")
            lo, hi = v[f"ci_pass_at_{kk}"]
            extra.append(
                rf"\newcommand{{\PassK{atag}{ntag}CI}}{{[{lo:.1f},{hi:.1f}]}}")
        red = passk["by_arm"][a].get("paired_reduction")
        if red:
            lo, hi = red["pass_at_1"]
            extra.append(
                rf"\newcommand{{\PairedDrop{atag}One}}{{[{lo:.1f},{hi:.1f}]}}")
            lo, hi = red[f"pass_at_{kk}"]
            extra.append(
                rf"\newcommand{{\PairedDrop{atag}K}}{{[{lo:.1f},{hi:.1f}]}}")
        pa = passk["by_arm"][a]
        extra.append(rf"\newcommand{{\Drop{atag}One}}{{"
                     rf"{pa['numeric']['pass_at_1'] - pa['provenance']['pass_at_1']:.1f}}}")
        extra.append(rf"\newcommand{{\Drop{atag}K}}{{"
                     rf"{pa['numeric'][f'pass_at_{kk}'] - pa['provenance'][f'pass_at_{kk}']:.1f}}}")

    extra.extend(review_macros(
        corpus_rows=corpus_rows, corpus_scored=corpus_scored,
        numeric_pass=numeric_pass, netlist_rows=netlists, key_of=key_of,
        verdicts_full=verdicts_full, is_fake=_is_fake, reverify=reverify,
        out_dir=out_dir, pred_by_key=pred_by_key))
    if _failures:
        print("INTEGRITY FAILURES:", file=sys.stderr)
        for f in _failures:
            print("  -", f, file=sys.stderr)
        return 1
    (dest / "extra.tex").write_text("\n".join(extra) + "\n")

    # ------------------------------------------------------------ new tables
    xt = ["% GENERATED by bench/reconcile.py -- do not edit by hand."]
    xt.append(r"""\begin{table}[t]
\centering
\caption{What each acceptance check contributes, over all """
              + str(len(numeric_pass)) + r""" numeric passes
  scored against the manual grades. Each rung is the union of the predicates
  named, so a rung can only add detections to the one above it;
  $\Delta$TP is what it adds. \emph{Resolved-binding provenance} is a single
  predicate --- the part name binds to exactly one vendor-supplied
  definition after transitive include resolution --- reported on its own line
  rather than as an increment, because on this population it is
  minimal-sufficient: no other predicate adds a detection to it.}
\label{tab:ladder}
\small
\begin{tabular}{@{}lccccc@{}}
\toprule
\textbf{Check} & \textbf{Flagged} & \textbf{TP} & \textbf{FP} &
  \textbf{Prec.} & \textbf{Rec.} \\
\midrule""")
    for sc in rungs:
        pr = f"{sc['precision']:.0f}\\%" if sc["precision"] is not None else "---"
        rc = f"{sc['recall']:.0f}\\%" if sc["recall"] is not None else "---"
        xt.append(rf"{sc['label']} & {sc['flagged']} & {sc['tp']} & {sc['fp']} "
                  rf"& {pr} & {rc} \\")
    xt.append(r"""\bottomrule
\end{tabular}
\end{table}
""")

    xt.append(r"""\begin{table}[t]
\centering
\caption{Pass@1 and Pass@""" + str(kk) + r""" under three acceptance criteria,
  pooled over models. \emph{Numeric} is Equation~\ref{eq:gate}.
  \emph{Provenance} additionally requires that the part name resolve to a
  vendor-supplied definition. \emph{Full protocol} additionally requires
  interface and waveform-integrity checks. Every (case, model, arm) cell has
  exactly $k=""" + str(kk) + r"""$ attempts and a filtered completion counts
  as a failed attempt, so the denominators are intention-to-treat. Cases are
  the unit of averaging.}
\label{tab:passk}
\small
\begin{tabular}{@{}lcccccc@{}}
\toprule
& \multicolumn{2}{c}{\textbf{Numeric}} & \multicolumn{2}{c}{\textbf{Provenance}}
& \multicolumn{2}{c}{\textbf{Full protocol}} \\
\cmidrule(lr){2-3}\cmidrule(lr){4-5}\cmidrule(lr){6-7}
\textbf{Arm} & Pass@1 & Pass@""" + str(kk) + r""" & Pass@1 & Pass@"""
              + str(kk) + r""" & Pass@1 & Pass@""" + str(kk) + r""" \\
\midrule""")
    for a in FULL_ARMS:
        cells = []
        for name in ("numeric", "provenance", "full_protocol"):
            v = passk["by_arm"][a][name]
            cells += [rf"{v['pass_at_1']:.1f}\%", rf"{v[f'pass_at_{kk}']:.1f}\%"]
        xt.append(rf"\textsc{{{a}}} & " + " & ".join(cells) + r" \\")
    xt.append(r"""\bottomrule
\end{tabular}
\end{table}
""")
    (dest / "extra_tables.tex").write_text("\n".join(xt) + "\n")

    print()
    print("=" * 72)
    print("CORPUS ACCOUNTING")
    print("=" * 72)
    print(f"  {corpus['total']} generations = {corpus['cases']} cases x "
          f"{corpus['models']} models x k={kk} x {corpus['arms']} arms")
    print(f"  {corpus['excluded_inspection']} excluded (inspection-only case, "
          f"{corpus['excluded_inspection'] // corpus['arms']} per arm)")
    print(f"  {corpus['scored']} scored = {corpus['per_arm_scored']} per arm")
    print()
    print("ACCEPTANCE LADDER (n=%d numeric passes)" % len(numeric_pass))
    for sc in rungs:
        pr = f"{sc['precision']:.0f}%" if sc["precision"] is not None else "  --"
        rc = f"{sc['recall']:.0f}%" if sc["recall"] is not None else "  --"
        print(f"  {sc['label'][:46]:<46} TP={sc['tp']:>3} FP={sc['fp']:>2} "
              f"P={pr:>4} R={rc:>4}  dTP={sc['delta_tp']:+d}")
    print(f"  minimal sufficient subset: {minimal['subset']} "
          f"(FP={minimal['fp']}, recall={minimal['recall']:.0f}%)")
    print()
    print("LEAVE-ONE-OUT (detections lost when each predicate is dropped)")
    for pred in ablation.PREDICATES:
        print(f"  {pred:<20} unique TP={loo[pred]['unique_tp']:>2}  "
              f"unique FP={loo[pred]['unique_fp']:>2}")
    print()
    print(f"Pass@1 / Pass@{kk}   (intention-to-treat)")
    for a in FULL_ARMS:
        line = f"  {a:<12}"
        for name in ("numeric", "provenance", "full_protocol"):
            v = passk["by_arm"][a][name]
            line += f"  {name}={v['pass_at_1']:.1f}/{v[f'pass_at_{kk}']:.1f}"
        print(line)
    print()
    print(f"  wrote {dest/'extra.tex'}")
    print(f"  wrote {dest/'extra_tables.tex'}")

    print(f"  wrote {dest/'numbers.tex'}")
    print(f"  wrote {dest/'tables.tex'}")
    print(f"  wrote {dest/'reconcile.json'}")
    print("  all integrity assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
