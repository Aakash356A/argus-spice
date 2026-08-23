#!/usr/bin/env python3
"""Check every probe case names a part that actually has an installed model.

The library-aware arms tell the model "the vendor macro-model is available".
If that is false for a part, the arm measures nothing: the model does the right
thing, the netlist cannot simulate, and the failure looks like evidence about
behaviour when it is really a missing file.

That is not hypothetical. Three cases in the first draft named ADP7112,
LTC6079 and LT6107 because `data/templates/<part>.net` exists for each -- but
those files are named for one part and instantiate a sibling (ADP7118, LTC6078,
LT6106). The named parts have no model at all, so an entire arm's worth of
generations was uninterpretable.

A case is valid when both hold:
  1. some corpus template actually instantiates that exact part, and
  2. the library that template loads is present in the LTspice install.

    python bench/validate_cases.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.cases import ALL_CASES, ProbeCase      # noqa: E402
from template_index import build_template_index   # noqa: E402

LIB_SEARCH_PATHS = [
    Path.home() / "Library/Application Support/LTspice/lib",
    Path("/Applications/LTspice.app/Contents/Resources/lib"),
    Path.home() / "Documents/LTspiceXVII/lib",
]


def installed_library_files() -> set[str]:
    names: set[str] = set()
    for root in LIB_SEARCH_PATHS:
        if root.is_dir():
            names |= {p.name.lower() for p in root.rglob("*") if p.is_file()}
    return names


def part_library(template_path: str) -> str:
    """The part-specific .lib/.include directive from a template."""
    for raw in Path(template_path).read_text(errors="ignore").splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line.lower().startswith((".lib", ".include", ".inc")):
            continue
        if "standard." in line.lower():
            continue
        return " ".join(line.split())
    return ""


def validate(cases: list[ProbeCase] | None = None) -> list[tuple[str, str, str]]:
    """Return (case_id, part, problem) for every case that cannot be trusted."""
    cases = cases or ALL_CASES
    index = build_template_index()
    installed = installed_library_files()
    problems = []
    for case in cases:
        paths = index.get(case.part.upper())
        if not paths:
            problems.append((case.case_id, case.part,
                             "no corpus template instantiates this part"))
            continue
        directive = part_library(paths[0])
        if not directive:
            problems.append((case.case_id, case.part,
                             "template loads no part-specific library"))
            continue
        filename = directive.split()[-1].lower()
        if installed and filename not in installed:
            problems.append((case.case_id, case.part,
                             f"library {filename!r} is not installed"))
    return problems


def main() -> int:
    index = build_template_index()
    installed = installed_library_files()
    if not installed:
        print("  warning: no LTspice library directory found; "
              "checking corpus coverage only.\n")

    print(f"{'case':<6}{'part':<14}{'.lib':<24}{'status'}")
    for case in ALL_CASES:
        paths = index.get(case.part.upper())
        directive = part_library(paths[0]) if paths else ""
        filename = directive.split()[-1].lower() if directive else ""
        if not paths:
            status = "NO TEMPLATE"
        elif not directive:
            status = "NO LIBRARY"
        elif installed and filename not in installed:
            status = "LIB NOT INSTALLED"
        else:
            status = "ok"
        print(f"{case.case_id:<6}{case.part:<14}{directive or '-':<24}{status}")

    problems = validate()
    print()
    if problems:
        print(f"{len(problems)} case(s) unusable in the library/strict arms:")
        for case_id, part, why in problems:
            print(f"  {case_id} {part}: {why}")
        return 1
    print(f"All {len(ALL_CASES)} cases valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
