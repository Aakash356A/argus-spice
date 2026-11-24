"""Verification tools as plain functions.

This module deliberately has no MCP dependency. `server.py` wraps it for
interactive use by an agent; `bench/` imports it directly for batch evaluation.
One implementation, so the tools an agent uses in a session are byte-for-byte
the tools the reported numbers come from -- a demo that diverges from the
harness measures nothing.

Every function returns a JSON-serialisable dict, and the failure paths carry
the detail an agent needs to make its next move: a measurement that cannot find
its node reports the node names that do exist, and a simulation that fails
reports the simulator's own diagnostic rather than a summary of it.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.simulation_runner import simulator_version             # noqa: E402
from bench.detect import audit as _audit                      # noqa: E402
from bench.provenance import resolve as _resolve              # noqa: E402
from sim_harness import Metric, Spec, parses_as_spice, run_spice  # noqa: E402
from spec_report import report as _report                     # noqa: E402
from template_index import find_template as _find_template    # noqa: E402

SUPPORTED_METRICS = ("gain_db", "bandwidth_hz", "vout_dc")

# A library directive may name a file, never a path. LTspice resolves a bare
# filename against its own library directories, which is what every netlist in
# the corpus does. Anything with a separator is a request to read an arbitrary
# location on disk, and these tools run a simulator over text supplied by a
# model -- so the netlist is untrusted input and the check is a real boundary,
# not a formality.
_LIB_DIRECTIVE = re.compile(r"^\s*\.(lib|inc|include)\s+(?P<target>\S+)",
                            re.IGNORECASE)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_library_safety(netlist: str) -> list[str]:
    """Reject library directives that reach outside the simulator's own tree."""
    violations = []
    for raw in netlist.splitlines():
        match = _LIB_DIRECTIVE.match(raw.split(";", 1)[0])
        if not match:
            continue
        target = match.group("target").strip().strip('"')
        if target.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", target):
            violations.append(f"absolute path in library directive: {target!r}")
        elif ".." in target:
            violations.append(f"parent-directory traversal in directive: {target!r}")
        elif "/" in target or "\\" in target:
            violations.append(f"path separator in library directive: {target!r}")
    return violations


def _guard(netlist: str) -> dict | None:
    """Shared precondition for anything that hands a netlist to the simulator."""
    if not netlist or not netlist.strip():
        return {"ok": False, "error": "empty netlist"}
    violations = check_library_safety(netlist)
    if violations:
        return {"ok": False, "error": "unsafe library directive",
                "violations": violations,
                "hint": "Reference a library by bare filename, e.g. '.lib ADI.lib'."}
    if not parses_as_spice(netlist):
        return {"ok": False, "error": "not a well-formed netlist",
                "hint": "Needs at least two element lines and a .end directive."}
    return None


def find_template(part: str) -> dict:
    """Locate a known-good reference netlist for a part.

    Also returns the library directive that supplies the part's macro-model,
    which is the single most useful fact for writing a netlist that runs: the
    filename differs per part and cannot be guessed.
    """
    path = _find_template(part)
    if not path:
        return {"ok": False, "error": f"no reference netlist for {part!r}"}
    netlist = Path(path).read_text(encoding="utf-8", errors="ignore")
    directive = ""
    for raw in netlist.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line.lower().startswith((".lib", ".inc", ".include")) \
                and "standard." not in line.lower():
            directive = " ".join(line.split())
            break
    return {"ok": True, "part": part, "path": str(path),
            "library_directive": directive, "netlist": netlist}


def simulate(netlist: str, timeout_s: int = 60) -> dict:
    """Run the netlist and report whether the simulator accepted it."""
    problem = _guard(netlist)
    if problem:
        return problem
    sim = run_spice(netlist, testbench="", timeout_s=timeout_s,
                    requested_measurements=set())
    log = (sim.raw_log or "").replace("\x00", "")
    if not sim.converged:
        return {"ok": False, "converged": False, "error": log[:2000],
                "hint": "The simulator's own message above names the fault."}
    return {"ok": True, "converged": True, "log": log[-1500:]}


def list_traces(netlist: str, timeout_s: int = 60) -> dict:
    """Simulate and report the node names the run actually produced.

    Vendor application netlists carry auto-generated node names such as `N001`,
    so the name a measurement needs usually cannot be inferred from the source.
    Without this an agent measures a node that does not exist and reads the
    resulting failure as a circuit fault rather than a naming mistake.
    """
    problem = _guard(netlist)
    if problem:
        return problem
    import measure_raw
    from app.simulation_runner import SimulationRunner

    runner = SimulationRunner()
    if not runner.is_available():
        return {"ok": False, "error": "LTspice executable not found"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "circuit.net"
        path.write_text(netlist, encoding="utf-8")
        result = runner.run_batch(path, timeout=timeout_s)
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("error", ""))[:1500]}
        traces = measure_raw.load_traces(result["raw_path"])
        names = [str(v) for v in (getattr(traces, "variables", []) or [])]
    return {"ok": True, "traces": names, "count": len(names)}


def measure(netlist: str, metrics: list[str], in_node: str = "IN",
            out_node: str = "OUT", freq_hz: float = 1000.0,
            timeout_s: int = 60) -> dict:
    """Measure named quantities from a simulation."""
    problem = _guard(netlist)
    if problem:
        return problem
    unknown = [m for m in metrics if m not in SUPPORTED_METRICS]
    if unknown:
        return {"ok": False, "error": f"unsupported metric(s): {unknown}",
                "supported": list(SUPPORTED_METRICS)}
    sim = run_spice(netlist, testbench="", timeout_s=timeout_s, in_node=in_node,
                    out_node=out_node, freq_hz=freq_hz,
                    requested_measurements=set(metrics))
    if not sim.converged:
        return {"ok": False, "converged": False,
                "error": (sim.raw_log or "").replace("\x00", "")[:2000]}
    out = {"ok": True, "converged": True, "measurements": sim.measurements,
           "diagnostics": list(sim.diagnostics)}
    if sim.diagnostics:
        # A missing measurement is nearly always a node-name mistake, and the
        # fix is one tool call away.
        out["hint"] = "Call list_traces to see the node names this run produced."
    return out


def verify_spec(netlist: str, metrics: list[dict], in_node: str = "IN",
                out_node: str = "OUT", freq_hz: float = 1000.0,
                timeout_s: int = 60) -> dict:
    """Measure and score against targets. `metrics`: [{name, target, tol}, ...]."""
    problem = _guard(netlist)
    if problem:
        return problem
    try:
        spec = Spec(metrics=[Metric(name=m["name"], target=float(m["target"]),
                                    tol=float(m.get("tol", 0.0)),
                                    direction=m.get("direction", "hit_target"))
                             for m in metrics], testbench="")
    except (KeyError, TypeError, ValueError) as error:
        return {"ok": False, "error": f"malformed metric spec: {error}",
                "expected": "[{'name': 'gain_db', 'target': 20.0, 'tol': 1.0}]"}
    sim = run_spice(netlist, testbench="", timeout_s=timeout_s, in_node=in_node,
                    out_node=out_node, freq_hz=freq_hz,
                    requested_measurements={m.name for m in spec.metrics})
    if not sim.converged:
        return {"ok": False, "converged": False,
                "error": (sim.raw_log or "").replace("\x00", "")[:2000]}
    reports = _report(sim, spec)
    return {"ok": True, "converged": True,
            "passed": bool(reports) and all(r.passed for r in reports),
            "reports": [{"name": r.name, "target": r.target,
                         "measured": r.measured, "passed": r.passed} for r in reports],
            "diagnostics": list(sim.diagnostics)}


def audit_netlist(netlist: str, part: str, out_node: str = "OUT",
                  metrics: list[str] | None = None) -> dict:
    """Resolve which definition the requested part name binds to.

    The qualification predicate is `provenance.vendor_supplied`: a reachable
    instance calls the part, no local definition claims the name, and exactly
    one vendor definition is reachable through the include graph. The
    syntactic diagnostics (substitution, stub, absence, pin role) are returned
    alongside it to explain a failure; they do not decide qualification.
    """
    res = _resolve(netlist, part)
    template = find_template(part)
    diag = _audit(netlist, part, out_node, metrics or [],
                  template.get("netlist", "") if template.get("ok") else "")
    return {"ok": True, "part": part,
            "verdict": "qualified" if res.vendor_supplied else "not_qualified",
            "provenance": res.evidence(),
            "diagnostics": diag.flags}


def submit(netlist: str, part: str, metrics: list[dict],
           spec_text: str = "", in_node: str = "IN", out_node: str = "OUT",
           freq_hz: float = 1000.0, timeout_s: int = 60) -> dict:
    """The acceptance gate of Eq. (2): numeric pass AND vendor binding.

    An evidence record is returned for every decision, accepted or not, so a
    rejection is as re-derivable as an acceptance.
    """
    verification = verify_spec(netlist, metrics, in_node, out_node,
                               freq_hz, timeout_s)
    audit = audit_netlist(netlist, part, out_node,
                          [m.get("name", "") for m in metrics])
    numeric = bool(verification.get("ok") and verification.get("passed"))
    qualified = audit["verdict"] == "qualified"
    accepted = numeric and qualified
    if accepted:
        reason = "numeric targets met and the part binds to one vendor definition"
    elif numeric:
        reason = f"numeric targets met, but binding is {audit['provenance']['binding']}"
    else:
        reason = "numeric targets not met"
    return {
        "ok": True,
        "accepted": accepted,
        "reason": reason,
        "evidence": {
            "part": part,
            "spec": spec_text,
            "netlist_sha256": _sha256(netlist),
            "simulator": simulator_version(),
            "numeric_pass": numeric,
            "reports": verification.get("reports", []),
            "provenance": audit["provenance"],
            "diagnostics": audit["diagnostics"],
        },
    }
