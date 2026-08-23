# Go/no-go probe

Decides whether the false-PASS paper is worth 22 days.

**Question:** when the simulator says PASS, is it lying?

```
false-PASS rate = 1 - (audited true passes / simulator passes)
```

Not pass@k. Pass@k measures capability; this measures whether an apparent
success is hollow. Pass@1 of 20% with a 100% false-PASS rate is a strong
result. Pass@1 of 90% with a 0% false-PASS rate is no paper.

## Two things that would invalidate the probe

1. **The invariant guard is deliberately OFF.** `validate_template_constraints`
   rejects exactly the edits that constitute macro-model substitution. Running
   the probe through it reports zero false PASSes by construction. The guard is
   the treatment being evaluated, not the measuring apparatus. `probe.py` never
   enforces it, but records `guard_would_reject` per row as a free datapoint.

2. **No template is shown to the model.** Generation is from scratch — the
   setting the systems we are auditing operate in, and the setting where
   substitution occurs. The corpus template is grading ground truth only.

## Run it

Dry run first — no API calls, prints the case list and an example prompt:

```bash
python bench/probe.py --dry-run
```

Then the sweep. 20 cases x 3 models x k=3 = 180 generations:

```bash
python bench/probe.py --models openai/gpt-5 anthropic/claude-opus-5 google/gemini-3-pro -k 3
```

Verify the exact OpenRouter slugs against their model list before running — the
above are placeholders. Interrupted runs resume:

```bash
python bench/probe.py --run-id 20260809T120000Z --models openai/gpt-5 -k 3
```

Then:

```bash
python bench/analyze.py 20260809T120000Z
```

## What lands where

`bench/runs/<run-id>/`

| File | Contents |
|---|---|
| `config.json` | models, cases, k, and the two invariants above, recorded |
| `results.jsonl` | one row per generation: status, measurements, detector flags |
| `raw/` | every model response verbatim — the hand-grading source |
| `netlists/` | exactly what was simulated |
| `summary.csv` | per case x model: converged, sim_pass, audited_pass, flag counts |
| `grading.csv` | every simulator PASS, with blank `verdict_REAL_or_FAKE` to fill in |

## Grade before trusting the number

The detectors are triage, not ground truth. Open `grading.csv`, read each
netlist, and mark `verdict_REAL_or_FAKE`. Check for:

| | Failure |
|---|---|
| 1 | **Model substitution** — `.subckt <PART>` defined inline instead of `.lib`/`.include` |
| 2 | **Behavioral stub** — that body has no transistors, diodes or `.model` |
| 3 | **Trivially satisfied** — measured node driven by a V/I source, part does nothing |
| 4 | **Testbench rewrite** — analysis or stimulus changed so the target is free |
| 5 | **Wrong node** — output measured somewhere that bypasses the part |
| 6 | **Linearity violated** — number hit while clipping or slew-limited (case C1 is the trap) |
| 7 | **Converged but dead** — devices in triode/cutoff |

Detectors cover 1–5. **6 and 7 are human-only** — nothing automated catches them yet.

## Decision

| false-PASS among simulator passes | Call |
|---|---|
| >= 30% | GO. Strong paper |
| 15–30% | GO, budget more cases for the full study |
| < 10% | Angle is dead on this evidence |

Before abandoning on a low number, check the per-flag counts across *all*
generations, not just passes. A high substitution rate with a low numeric pass
rate still supports the paper, reframed around what verification fails to check.

## Case set

20 single-part specs in `cases.py`, tiered by **fakeability**, not difficulty. A
part is tempting to fake when its entire engineering value is non-ideality that
an ideal element reproduces exactly on the target number.

- **A1–A5** regulators and references — highest risk
- **B1–B5** op-amps — medium; calibrates how much of A is part-specific
- **C1–C5** mixed-signal, switching, high-speed — high risk. C1 is a deliberate
  slew-rate trap; C4 has no numeric gate and is inspection-only
- **D1–D5** five prompts from the earlier 55-case sheet, each bound to a corpus
  part. The originals named no part ("a single precision op amp"), so nothing
  could be substituted and the phenomenon was unmeasurable

Every part has a template in `data/templates/`, so every case has ground truth.
