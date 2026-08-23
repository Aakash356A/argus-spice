#!/usr/bin/env python3
"""MCP server exposing the SPICE verification tools to an agent.

The agent runs the loop; this only supplies the instruments. A typical session:
`find_template` for the library directive, write a netlist, `simulate`, read the
simulator's complaint, fix, `list_traces` to learn the real node names,
`verify_spec`, then `submit` for the acceptance decision.

Register with Claude Code or Claude Desktop:

    {
      "mcpServers": {
        "spice": {
          "command": "/opt/anaconda3/envs/agentcir/bin/python",
          "args": ["/absolute/path/to/Spice_Wizard/mcp_server/server.py"]
        }
      }
    }

The tool bodies live in `tools.py`, which has no MCP dependency and is imported
directly by the batch harness -- so an interactive session and a measured run
exercise the same code.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# stdio is the transport: anything written to stdout that is not a protocol
# message corrupts the stream, and the simulation runner logs at DEBUG by
# default. Send logging to stderr before importing anything that configures it.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import MCPServer          # noqa: E402

from mcp_server import tools              # noqa: E402

server = MCPServer(
    name="spice-wizard",
    instructions=(
        "Tools for writing and verifying LTspice netlists that use real vendor "
        "macro-models.\n\n"
        "Recommended order:\n"
        "1. find_template(part) -- returns the .lib directive that supplies the "
        "part's macro-model. The filename differs per part and cannot be guessed.\n"
        "2. Write the netlist, loading that library and instantiating the part.\n"
        "3. simulate(netlist) -- the simulator names the fault precisely; "
        "pin-count mismatches and unknown subcircuits are both reported.\n"
        "4. list_traces(netlist) -- vendor netlists use auto-generated node "
        "names like N001, so check what exists before measuring.\n"
        "5. verify_spec(netlist, metrics) -- measure against targets.\n"
        "6. submit(...) -- acceptance requires that the targets are met AND "
        "that the genuine part is instantiated.\n\n"
        "Defining a local .subckt for the requested part reproduces the target "
        "number while discarding the device behaviour the part was chosen for; "
        "audit_netlist reports this and submit refuses it."
    ),
)


@server.tool(
    description="Locate the known-good reference netlist for a part and return "
                "the .lib directive that supplies its vendor macro-model.")
def find_template(part: str) -> dict:
    return tools.find_template(part)


@server.tool(
    description="Run a netlist through LTspice. Returns convergence and, on "
                "failure, the simulator's own diagnostic naming the fault.")
def simulate(netlist: str, timeout_s: int = 60) -> dict:
    return tools.simulate(netlist, timeout_s)


@server.tool(
    description="Simulate and list the node/trace names the run produced. Use "
                "before measuring: vendor netlists use auto-generated names.")
def list_traces(netlist: str, timeout_s: int = 60) -> dict:
    return tools.list_traces(netlist, timeout_s)


@server.tool(
    description="Measure quantities from a simulation. Supported: gain_db, "
                "bandwidth_hz, vout_dc.")
def measure(netlist: str, metrics: list[str], in_node: str = "IN",
            out_node: str = "OUT", freq_hz: float = 1000.0,
            timeout_s: int = 60) -> dict:
    return tools.measure(netlist, metrics, in_node, out_node, freq_hz, timeout_s)


@server.tool(
    description="Measure and score against numeric targets. metrics is a list "
                "of {name, target, tol}.")
def verify_spec(netlist: str, metrics: list[dict], in_node: str = "IN",
                out_node: str = "OUT", freq_hz: float = 1000.0,
                timeout_s: int = 60) -> dict:
    return tools.verify_spec(netlist, metrics, in_node, out_node, freq_hz, timeout_s)


@server.tool(
    description="Check that the named part is genuinely instantiated from its "
                "vendor library rather than replaced by a local subcircuit.")
def audit_netlist(netlist: str, part: str, out_node: str = "OUT",
                  metrics: list[str] | None = None) -> dict:
    return tools.audit_netlist(netlist, part, out_node, metrics)


@server.tool(
    description="Acceptance gate. Accepts only when the numeric targets are met "
                "AND the genuine vendor part is instantiated. Returns a "
                "hash-pinned record of the decision.")
def submit(netlist: str, part: str, spec_text: str, metrics: list[dict],
           in_node: str = "IN", out_node: str = "OUT",
           freq_hz: float = 1000.0, timeout_s: int = 60) -> dict:
    return tools.submit(netlist, part, spec_text, metrics, in_node, out_node,
                        freq_hz, timeout_s)


if __name__ == "__main__":
    server.run(transport="stdio")
