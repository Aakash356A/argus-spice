# GUI desktop application

A Tkinter application for drafting and verifying LTspice netlists: describe a
circuit, have a model generate a netlist, simulate it, plot the traces, and score
it against numeric targets.

This is **not** part of the contribution of the paper in the repository root. It
predates that work and is kept because it shares the same verification spine —
`sim_harness.run_spice()`, `measure_raw`, `spec_report` and
`app/simulation_runner.py`. Changes to those modules affect both.

## Running it

```bash
pip install -r ../requirements.txt
python app/run_gui.py
```

Set `OPENROUTER_API_KEY` in `.env` at the repository root (see `.env.example`)
for the generation features. Simulation, plotting and spec scoring work without
a key.

## Layout

| File | Role |
|---|---|
| `run_gui.py` | entry point |
| `gui_main.py` | main window, tabs, and event wiring |
| `agent_core.py` | intent routing and generation planning |
| `netlist_editor.py` | netlist text editing |
| `plot_manager.py` | waveform plotting |
| `ft_mac.py` | optional local Gemma adapter backend |
| `simulation_runner.py` | LTspice invocation — **shared with the paper harness** |

## Two optional assets are not tracked in git

Both are excluded by `.gitignore`; the application degrades gracefully without
them rather than failing to start.

**`app/available_ics.txt`** — a shortlist of parts with installed macro-models,
used to suggest ICs for a request. Without it, `_load_ic_database()` returns an
empty list and suggestions are simply not offered. Any plain list of part
numbers, one per line, works.

**`models/gemma3-lora/`** — a LoRA adapter for the optional local-inference
backend in `ft_mac.py`. Without it, that backend raises a clear
`FileNotFoundError` on construction; every other path, including the default
hosted-model route, is unaffected.

## Relationship to the paper

The paper studies whether a simulation-based acceptance gate establishes that a
generated netlist actually instantiates the vendor part it names. This
application is a consumer of that same gate, not evidence for the paper's
claims, and no result in the paper depends on it.
