#!/usr/bin/env python3
"""Write the prompt manifest for a run: the exact text each slot was sent.

    python bench/prompt_manifest.py 20260809T211634Z

The run records carry the arm (`prompt_variant`) but not the prompt text, and
their `lib_directive_supplied` field is empty for every LIBRARY slot, so the
directive the LIBRARY prompt supplied is not recoverable from the records
alone. Prompts are a deterministic function of the case definition, the arm,
and the part's reference template (`probe.build_prompt`), so this script
reconstructs each one from the released code and records it with its SHA-256.
One entry per (case, arm); every model and sample in a cell received the same
prompt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cases import ALL_CASES  # noqa: E402
from probe import (SYSTEM_PROMPT, _load_template, build_prompt,  # noqa: E402
                   extract_lib_directive)

ARMS = ("bare", "requirement", "library")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    args = ap.parse_args()
    run = Path(__file__).resolve().parent / "runs" / args.run_id
    seen = {(json.loads(l).get("case_id"), json.loads(l).get("prompt_variant", "bare"))
            for l in (run / "results.jsonl").read_text().splitlines() if l.strip()}
    rows = []
    for case in ALL_CASES:
        directive = extract_lib_directive(_load_template(case.part))
        for arm in ARMS:
            if (case.case_id, arm) not in seen:
                continue
            prompt = build_prompt(case, arm, directive)
            rows.append({
                "case_id": case.case_id, "arm": arm, "part": case.part,
                "lib_directive": directive if arm == "library" else "",
                "system_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "prompt": prompt,
            })
    out = run / "prompts.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"wrote {len(rows)} prompts -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
