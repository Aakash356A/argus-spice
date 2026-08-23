#!/usr/bin/env python3
"""Arm 3: generation with verification tools and a repair loop.

Arms 1 and 2 (`bench/probe.py`) are single-shot: the model writes a netlist and
we score whatever it produced. This arm gives the model the same tools an agent
would have through the MCP server and lets it iterate -- write, simulate, read
the simulator's complaint, fix, re-simulate -- before anything is scored.

The prediction this arm tests is specific. In the `library` arm 59 generations
died on `Port(pin) count mismatch` and 53 more carried a pin-role error the
simulator accepts silently. Both are faults LTspice names precisely, so both are
the kind a feedback loop should repair. If tool access converts a meaningful
share of them into genuine acceptances, the protocol is not only a detector but
a fix.

Tool bodies come from `mcp_server.tools`, the same module the MCP server wraps,
so this arm and an interactive session exercise identical code.

    python bench/probe_tools.py --models anthropic/claude-opus-5 -k 3
    python bench/probe_tools.py --cases A1 B1 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bench.cases import ProbeCase, select                     # noqa: E402
from bench.detect import audit                                # noqa: E402
from bench.validate_cases import validate as validate_cases   # noqa: E402
from llm_client import LLMError, call_llm, extract_netlist    # noqa: E402
from mcp_server import tools                                  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parent / "runs"
PROMPT_VARIANT = "tools"

SYSTEM_PROMPT = (
    "You are an expert analog applications engineer who writes LTspice netlists. "
    "You have access to a simulator and may iterate until the netlist runs and "
    "meets its targets."
)

# The tool surface described to the model. Kept in lockstep with
# mcp_server/server.py's instructions -- if the two drift, this arm stops
# measuring what the MCP server does.
TOOL_MENU = """\
You can call these tools. Emit exactly one call per reply, as a single JSON
object on its own line and nothing else:

  {"tool": "find_template", "part": "<part>"}
      Returns the .lib directive that supplies the part's vendor macro-model.
      The filename differs per part and cannot be guessed.

  {"tool": "simulate", "netlist": "<full netlist>"}
      Runs it. On failure returns the simulator's own diagnostic.

  {"tool": "list_traces", "netlist": "<full netlist>"}
      Node names the run produced. Vendor netlists use auto-generated names
      such as N001, so check before measuring.

  {"tool": "verify_spec", "netlist": "<full netlist>"}
      Measures against this task's targets and reports pass/fail.

  {"tool": "submit", "netlist": "<full netlist>"}
      Final answer. Accepted only if the targets are met AND the genuine
      vendor part is instantiated.

Call submit when you believe the netlist is correct."""


def build_prompt(case: ProbeCase) -> str:
    targets = "\n".join(f"  - {n}: target {t} (tolerance +/-{tol})"
                        for n, t, tol in case.metrics) or "  - (graded by inspection)"
    return f"""Write a complete, simulation-ready LTspice netlist for the following requirement.

PART: {case.part}
REQUIREMENT: {case.spec}

NUMERIC TARGETS:
{targets}

MANDATORY CONVENTIONS:
  - Name the primary input node exactly: {case.in_node}
  - Name the primary output node exactly: {case.out_node}
  - Include an analysis directive that lets the targets be measured.
  - End the netlist with .end

{TOOL_MENU}
"""


def parse_tool_call(text: str) -> dict | None:
    """Pull a single JSON tool call out of a reply.

    Models fence JSON, prepend prose, or emit the object bare; all three are
    accepted. Scanning from the last '{' finds the call even when the model
    narrates first, which it often does.
    """
    stripped = text.strip()
    if "```" in stripped:
        blocks = stripped.split("```")
        for block in blocks[1::2]:
            body = block.split("\n", 1)[-1] if "\n" in block else block
            try:
                call = json.loads(body.strip())
                if isinstance(call, dict) and "tool" in call:
                    return call
            except json.JSONDecodeError:
                continue
    starts = [i for i, ch in enumerate(stripped) if ch == "{"]
    for start in reversed(starts):
        try:
            call = json.loads(stripped[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(call, dict) and "tool" in call:
            return call
    return None


def dispatch(call: dict, case: ProbeCase) -> dict:
    """Execute one tool call against the shared tool layer."""
    name = call.get("tool")
    netlist = call.get("netlist", "")
    metrics = [{"name": n, "target": t, "tol": tol} for n, t, tol in case.metrics]
    if name == "find_template":
        return tools.find_template(call.get("part", case.part))
    if name == "simulate":
        return tools.simulate(netlist)
    if name == "list_traces":
        return tools.list_traces(netlist)
    if name == "verify_spec":
        return tools.verify_spec(netlist, metrics, case.in_node, case.out_node,
                                 case.freq_hz)
    if name == "submit":
        return tools.submit(netlist, case.part, case.spec, metrics,
                            case.in_node, case.out_node, case.freq_hz)
    return {"ok": False, "error": f"unknown tool {name!r}",
            "available": ["find_template", "simulate", "list_traces",
                          "verify_spec", "submit"]}


def run_one(case: ProbeCase, model: str, sample: int, out_dir: Path,
            max_turns: int, temperature: float | None, timeout_s: int,
            max_tokens: int) -> dict:
    """Drive one generate-verify-repair episode to a submission or turn limit."""
    record = {
        "case_id": case.case_id, "group": case.group, "part": case.part,
        "fake_risk": case.fake_risk, "model": model, "sample": sample,
        "prompt_variant": PROMPT_VARIANT,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manual_grade_only": case.manual_grade_only,
    }
    stem = f"{case.case_id}_{''.join(c if c.isalnum() or c in '-_.' else '-' for c in model)}_tools_k{sample}"
    started = time.monotonic()
    transcript: list[dict] = []
    conversation = build_prompt(case)
    last_netlist = ""

    for turn in range(1, max_turns + 1):
        try:
            reply = call_llm(conversation, system_prompt=SYSTEM_PROMPT,
                             model=model, temperature=temperature,
                             timeout_s=timeout_s, max_tokens=max_tokens)
        except LLMError as error:
            record.update(status="LLM_ERROR", error=str(error), turns=turn,
                          elapsed_sec=time.monotonic() - started)
            break

        call = parse_tool_call(reply)
        if call is None:
            # No call: treat any netlist in the reply as an implicit submission
            # rather than discarding the turn.
            candidate = extract_netlist(reply)
            if candidate.strip():
                call = {"tool": "submit", "netlist": candidate}
            else:
                conversation += (f"\n\nASSISTANT: {reply[:400]}\n\nEmit exactly one "
                                 "JSON tool call and nothing else.")
                transcript.append({"turn": turn, "tool": None, "note": "no call parsed"})
                continue

        if call.get("netlist"):
            last_netlist = call["netlist"]
        result = dispatch(call, case)
        transcript.append({"turn": turn, "tool": call.get("tool"),
                           "result": {k: v for k, v in result.items()
                                      if k != "netlist"}})

        if call.get("tool") == "submit":
            record.update(status="SUBMITTED", accepted=bool(result.get("accepted")),
                          metrics_passed=result.get("metrics_passed"),
                          audit_verdict=result.get("audit_verdict"),
                          audit_flags=result.get("audit_flags", []),
                          reports=result.get("reports", []),
                          turns=turn, elapsed_sec=time.monotonic() - started)
            break

        conversation += (f"\n\nASSISTANT: {json.dumps(call)[:300]}"
                         f"\n\nTOOL RESULT: {json.dumps(result)[:2500]}")
    else:
        record.update(status="TURN_LIMIT", turns=max_turns,
                      elapsed_sec=time.monotonic() - started)

    if last_netlist:
        (out_dir / "netlists" / f"{stem}.net").write_text(last_netlist, encoding="utf-8")
        record["netlist_file"] = f"netlists/{stem}.net"
        a = audit(last_netlist, case.part, case.out_node,
                  [n for n, _, _ in case.metrics])
        record["audit"] = a.to_dict()
    (out_dir / "raw" / f"{stem}.json").write_text(
        json.dumps(transcript, indent=2), encoding="utf-8")
    record["transcript_file"] = f"raw/{stem}.json"
    record["tool_calls"] = [t.get("tool") for t in transcript]
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=["anthropic/claude-opus-5"])
    parser.add_argument("-k", "--samples", type=int, default=3)
    parser.add_argument("--cases", nargs="*", default=None)
    parser.add_argument("--groups", nargs="*", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--max-turns", type=int, default=8,
                        help="Tool calls per episode before giving up.")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-tokens", type=int, default=32000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cases = select(args.cases, args.groups)
    if not cases:
        parser.error("No cases matched.")

    if args.dry_run:
        print(f"{len(cases)} case(s); {len(cases)*len(args.models)*args.samples} "
              f"episode(s), up to {args.max_turns} tool calls each.")
        print(f"\n--- prompt ({cases[0].case_id}) ---\n")
        print(build_prompt(cases[0]))
        return 0

    problems = validate_cases(cases)
    if problems:
        print("These cases name parts with no installed model:")
        for case_id, part, why in problems:
            print(f"  {case_id} {part}: {why}")
        return 2

    print("Preflight: simulating a known-good netlist ...", flush=True)
    check = tools.simulate(
        Path(tools.find_template("AD8092")["path"]).read_text(errors="ignore"))
    if not check.get("ok"):
        print(f"Simulator not working; no API calls made.\n  {check.get('error','')[:300]}")
        return 2
    print("Preflight: OK\n")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = RUNS_DIR / run_id
    for sub in ("raw", "netlists"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"

    done = set()
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done.add((row.get("case_id"), row.get("model"), row.get("sample"),
                          row.get("prompt_variant", "bare")))

    pending = [(c, m, k) for c in cases for m in args.models
               for k in range(1, args.samples + 1)
               if (c.case_id, m, k, PROMPT_VARIANT) not in done]
    print(f"  {len(pending)} episode(s) to run.\n")

    with results_path.open("a", encoding="utf-8") as sink:
        for index, (case, model, sample) in enumerate(pending, start=1):
            label = f"[{index}/{len(pending)}] tools {case.case_id} {model} k{sample}"
            print(f"{label} ...", flush=True)
            record = run_one(case, model, sample, out_dir, args.max_turns,
                             args.temperature, args.timeout, args.max_tokens)
            sink.write(json.dumps(record) + "\n")
            sink.flush()
            print(f"{label} {record.get('status')} "
                  f"accepted={record.get('accepted')} "
                  f"turns={record.get('turns')} "
                  f"calls={'>'.join(t for t in record.get('tool_calls', []) if t)}")

    print(f"\nWrote {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
