"""What each acceptance check contributes, and Pass@k under each of them.

Two questions a reader should not have to take on trust.

1. *Which check is doing the work?* Reporting one aggregate precision/recall
   for a four-predicate audit invites the suspicion that a single trivial
   check carries it. So every rung of the ladder is scored separately against
   the same manual grades, each predicate is scored standalone, and each is
   removed from the full audit in turn.

2. *What does Pass@k look like once provenance is required?* The paper argues
   that a numeric Pass@k overstates what was verified; that argument is worth
   more with the two numbers side by side than as a claim.

Both are computed from the stored corpus with no simulation and no API calls.
Denominators are intention-to-treat: a generation that returned no completion
counts as a failure of its arm, never as a missing observation.
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import random                              # noqa: E402

from cases import BY_ID                    # noqa: E402
from provenance import resolve             # noqa: E402

BOOTSTRAP_ITERS = 10000
BOOTSTRAP_SEED = 20260813

# The audit's accept/reject predicates, in the order the ladder introduces
# them. `resolved_binding` is not a field of the stored audit -- it is
# recomputed here from the netlist and the installed libraries.
PREDICATES = ("no_part", "substituted", "behavioral_stub",
              "resolved_binding", "pin_role_mismatch")

PREDICATE_LABEL = {
    "no_part": "requested part never instantiated",
    "substituted": "local same-name \\texttt{.subckt}",
    "behavioral_stub": "local definition with no device content",
    "resolved_binding": "name does not resolve to a vendor model",
    "pin_role_mismatch": "pin role disagrees with reference",
}

# The cumulative ladder: each rung is the union of the predicates
# named, so a rung can only add detections to the one above it.
LADDER = [
    ("numeric simulation alone", ()),
    ("+ requested-part instance check", ("no_part",)),
    ("+ local same-name \\texttt{.subckt}", ("no_part", "substituted")),
    ("+ behavioural-stub refinement",
     ("no_part", "substituted", "behavioral_stub")),
    ("resolved-binding provenance (alone)", ("resolved_binding",)),
    ("+ pin-role validation", ("resolved_binding", "pin_role_mismatch")),
    ("full audit", PREDICATES),
]


def predicate_values(row, netlist: str) -> dict:
    """Every accept/reject predicate for one netlist, re-derived from disk."""
    case = BY_ID[row["case_id"]]
    audit = row.get("audit") or {}
    res = resolve(netlist, case.part)
    return {
        "no_part": bool(audit.get("no_part")),
        "substituted": bool(audit.get("substituted")),
        "behavioral_stub": bool(audit.get("behavioral_stub")),
        # Fires when the part name does not resolve to exactly one
        # vendor-supplied definition -- local, shadowed, duplicated or
        # unresolved all count as provenance not established.
        "resolved_binding": not res.vendor_supplied,
        "pin_role_mismatch": bool(audit.get("pin_role_mismatch")),
        "_resolution": res,
    }


def score(rows, preds_by_key, key_of, is_fake, active: tuple) -> dict:
    """Confusion matrix for the union of `active` predicates."""
    tp = fp = fn = tn = 0
    for r in rows:
        flagged = any(preds_by_key[key_of(r)][p] for p in active)
        fake = is_fake(r)
        if flagged and fake:
            tp += 1
        elif flagged and not fake:
            fp += 1
        elif not flagged and fake:
            fn += 1
        else:
            tn += 1
    n = tp + fp + fn + tn
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": n,
        "precision": (100.0 * tp / (tp + fp)) if (tp + fp) else None,
        "recall": (100.0 * tp / (tp + fn)) if (tp + fn) else None,
        "flagged": tp + fp,
    }


def ladder(rows, preds_by_key, key_of, is_fake) -> list[dict]:
    out = []
    previous = 0
    for label, active in LADDER:
        s = score(rows, preds_by_key, key_of, is_fake, active)
        s["label"] = label
        s["active"] = list(active)
        s["delta_tp"] = s["tp"] - previous
        previous = s["tp"]
        out.append(s)
    return out


def standalone(rows, preds_by_key, key_of, is_fake) -> dict:
    return {p: score(rows, preds_by_key, key_of, is_fake, (p,))
            for p in PREDICATES}


def leave_one_out(rows, preds_by_key, key_of, is_fake) -> dict:
    """Each predicate removed from the full audit in turn.

    `unique_tp` is what only this predicate catches: the detections lost when
    it is dropped. A predicate with `unique_tp == 0` is fully redundant on
    this corpus, which is a result worth printing rather than hiding.
    """
    full = score(rows, preds_by_key, key_of, is_fake, PREDICATES)
    out = {}
    for p in PREDICATES:
        rest = tuple(q for q in PREDICATES if q != p)
        s = score(rows, preds_by_key, key_of, is_fake, rest)
        s["unique_tp"] = full["tp"] - s["tp"]
        s["unique_fp"] = full["fp"] - s["fp"]
        s["removed"] = p
        out[p] = s
    out["_full"] = full
    return out


def minimal_sufficient(rows, preds_by_key, key_of, is_fake) -> dict:
    """Smallest predicate subset matching the full audit's recall.

    States directly which predicates carry the detections, rather than
    leaving it to be inferred from the ladder.
    """
    full = score(rows, preds_by_key, key_of, is_fake, PREDICATES)
    for size in range(1, len(PREDICATES) + 1):
        best = None
        for subset in combinations(PREDICATES, size):
            s = score(rows, preds_by_key, key_of, is_fake, subset)
            if s["recall"] is not None and full["recall"] is not None \
               and s["recall"] >= full["recall"]:
                if best is None or s["fp"] < best["fp"]:
                    best = dict(s, subset=list(subset))
        if best:
            return best
    return dict(full, subset=list(PREDICATES))


# --------------------------------------------------------------------------
# Pass@k
# --------------------------------------------------------------------------
def stage_matrix(rows, preds_by_key, key_of, is_fake, wave_rejects) -> list[dict]:
    """Acceptance, false acceptance and valid acceptance at each protocol stage.

    Separate tables did not show whether the waveform check
    removes anything beyond provenance and interface, because both the
    per-stage counts and the population each stage operates on were implicit.
    Each row here is the *cumulative* gate: a candidate is accepted only if it
    clears every stage up to and including that row.
    """
    def gate_prov(r):
        return not preds_by_key[key_of(r)]["resolved_binding"]

    def gate_iface(r):
        return gate_prov(r) and not preds_by_key[key_of(r)]["pin_role_mismatch"]

    def gate_wave(r):
        return gate_iface(r) and not wave_rejects(r)

    stages = [("numeric gate", lambda r: True),
              ("$+$ resolved binding", gate_prov),
              ("$+$ interface check", gate_iface),
              ("$+$ waveform integrity", gate_wave)]
    out, previous = [], None
    for label, gate in stages:
        accepted = [r for r in rows if gate(r)]
        false_acc = sum(1 for r in accepted if is_fake(r))
        row = {"stage": label, "accepted": len(accepted),
               "false_accepted": false_acc,
               "valid_accepted": len(accepted) - false_acc,
               "removed": None if previous is None else previous - len(accepted)}
        previous = len(accepted)
        out.append(row)
    return out


def pass_at_k(rows, key_of, criteria: dict, k: int = 3) -> dict:
    """Pass@1 and Pass@k per criterion, per model and arm, and pooled.

    Every (case, model, arm) cell has exactly `k` attempted generations. A
    generation with no completion is a failure, not a missing value, so both
    estimators use `k` as the denominator regardless of what came back:
    Pass@1 is the mean success rate over attempts, Pass@k the fraction of
    cells with at least one success. Cells are the unit of averaging, so a
    case with more passes cannot dominate.
    """
    cells: dict[tuple, dict[str, list[bool]]] = {}
    for r in rows:
        cell = (r.get("prompt_variant", "bare"), r["model"], r["case_id"])
        bucket = cells.setdefault(cell, {name: [] for name in criteria})
        for name, fn in criteria.items():
            bucket[name].append(bool(fn(r)))

    def summarise(selected):
        out = {}
        for name in criteria:
            per_cell = [c[name] for c in selected]
            if not per_cell:
                out[name] = {"pass_at_1": None, f"pass_at_{k}": None, "cells": 0}
                continue
            p1 = sum(sum(v) / k for v in per_cell) / len(per_cell)
            pk = sum(1 for v in per_cell if any(v)) / len(per_cell)
            out[name] = {"pass_at_1": 100.0 * p1, f"pass_at_{k}": 100.0 * pk,
                         "cells": len(per_cell)}
        return out

    arms = sorted({c[0] for c in cells})
    models = sorted({c[1] for c in cells})
    result = {"by_arm": {}, "by_arm_model": {}, "k": k}
    for a in arms:
        result["by_arm"][a] = summarise([v for c, v in cells.items() if c[0] == a])
        for m in models:
            sel = [v for c, v in cells.items() if c[0] == a and c[1] == m]
            if sel:
                result["by_arm_model"].setdefault(a, {})[m] = summarise(sel)

    # ---- paired case-clustered bootstrap -------------------------------
    # The criteria are evaluated on the *same* generations, so resampling
    # them independently would overstate the uncertainty of their
    # difference. Each replicate draws one set of cases and scores every
    # criterion on it, which is what makes the interval on the reduction a
    # paired interval rather than two marginal ones subtracted.
    for a in arms:
        arm_cells = {c: v for c, v in cells.items() if c[0] == a}
        case_ids = sorted({c[2] for c in arm_cells})
        by_case = {}
        for c, v in arm_cells.items():
            by_case.setdefault(c[2], []).append(v)
        rng = random.Random(BOOTSTRAP_SEED)
        draws = {name: {"p1": [], "pk": []} for name in criteria}
        diffs = {"p1": [], "pk": []}
        for _ in range(BOOTSTRAP_ITERS):
            picked = []
            for _ in range(len(case_ids)):
                picked.extend(by_case[case_ids[rng.randrange(len(case_ids))]])
            est = {}
            for name in criteria:
                per_cell = [c[name] for c in picked]
                est[name] = (100.0 * sum(sum(v) / k for v in per_cell) / len(per_cell),
                             100.0 * sum(1 for v in per_cell if any(v)) / len(per_cell))
                draws[name]["p1"].append(est[name][0])
                draws[name]["pk"].append(est[name][1])
            if "numeric" in est and "provenance" in est:
                diffs["p1"].append(est["numeric"][0] - est["provenance"][0])
                diffs["pk"].append(est["numeric"][1] - est["provenance"][1])

        def interval(values):
            values = sorted(values)
            return (values[int(0.025 * len(values))],
                    values[int(0.975 * len(values)) - 1])

        for name in criteria:
            result["by_arm"][a][name]["ci_pass_at_1"] = interval(draws[name]["p1"])
            result["by_arm"][a][name][f"ci_pass_at_{k}"] = interval(draws[name]["pk"])
        if diffs["p1"]:
            result["by_arm"][a]["paired_reduction"] = {
                "pass_at_1": interval(diffs["p1"]),
                f"pass_at_{k}": interval(diffs["pk"]),
            }
    return result
