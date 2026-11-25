# Audit harness

Generates candidate netlists under controlled prompt conditions, simulates
them, audits their provenance, and scores every acceptance criterion the paper
reports. All results derive from the stored run in `runs/20260809T211634Z/`;
nothing here requires API access unless you generate a new corpus.

## Design invariants

1. **No template is shown to the model.** Generation is from scratch — the
   setting the audited systems operate in, and the setting where substitution
   occurs. The corpus template is grading ground truth only.

2. **The audit is not enforced during generation.** `probe.py` records what
   the audit *would* say per row, but acceptance during the sweep is the
   numeric gate alone — the audit is the object of evaluation, not part of
   the measuring apparatus.

3. **Analysis re-derives from disk.** `analyze.py` re-audits every stored
   netlist on every invocation rather than trusting cached fields, so a
   detector change re-scores the whole corpus for free.

## Files

| File | Role |
|---|---|
| `cases.py` | the 20 single-part specifications, stratified by fake-risk |
| `probe.py` | generation sweep (API access required; not needed to reproduce) |
| `detect.py` | syntactic audit signals |
| `provenance.py` | transitive `.lib`/`.include` resolution |
| `baselines.py` | name, include and direct-digest baselines |
| `samename.py` | same name, different model: three mechanisms with matched controls |
| `fixtures.py`, `collateral.py` | evaluator-owned fixtures; variants, exports, precedence |
| `timing.py` | resolver timing |
| `ablation.py` | acceptance ladder, leave-one-out, Pass@k |
| `mutants.py` | held-out mutation suite |
| `analyze.py` | re-audit and per-case tables |
| `reconcile.py` | result counts, rates and tables, integrity-checked |
| `waveform.py` | THD/clipping linearity measurement |
| `grade.py` | interactive grading tool; each record in `verdicts.jsonl` states its own `basis` |
| `reverify.py` | mechanical re-verification of every valid grade |
| `purge.py` | drops invalid rows so `probe.py` regenerates them on resume |
| `validate_cases.py` | preflight: every case's part has an installed library |

## Reproduce

```bash
python bench/validate_cases.py
python bench/reconcile.py 20260809T211634Z --out generated
python bench/mutants.py --parts 40
python bench/case_table.py 20260809T211634Z
```

`reconcile.py` recomputes static resolution, so it needs the LTspice library
pinned in `data/library_snapshot.json` installed and refuses to start without
it; it runs no simulation. `reverify.py` also needs LTspice 17.2.4.
