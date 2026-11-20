# SPICE verification MCP server

Exposes the acceptance protocol as tools an agent can call. The agent runs the
loop — write, simulate, read the fault, fix, re-simulate — and these supply the
instruments.

## Why it exists

A netlist that meets its numeric target has not thereby been shown to use the
part it names. Both of these measure ~13.97 dB against a 13.98 dB target:

| Netlist | Numeric gate | This protocol |
|---|---|---|
| Local `.subckt AD8092` built from an E-source | pass | **rejected** — binding `local` (diagnostics: `substituted`, `behavioral_stub`) |
| `.lib ADI.lib` + genuine `AD8092` instance | pass | **accepted** |

`submit` accepts only when both hold: targets met **and** the part name binds
to exactly one vendor-supplied definition (`bench/provenance.py`). It returns a
hash-pinned evidence record for every decision, accepted or not.

## Register with Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "spice": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/argus-spice/mcp_server/server.py"]
    }
  }
}
```

Use the `agentcir` interpreter — the default `python` lacks the `ltspice`
package, and the server will fail on first simulate.

## Tools

| Tool | Purpose |
|---|---|
| `find_template(part)` | Reference netlist **and the `.lib` directive that supplies the macro-model**. The filename differs per part (`ADI.lib`, `LT3045-1.sub`, `ADP7102.lib`) and cannot be guessed. |
| `simulate(netlist)` | Run it. On failure returns the simulator's own diagnostic — pin-count mismatches and unknown subcircuits are both named precisely. |
| `list_traces(netlist)` | Node names the run actually produced. Vendor netlists use auto-generated names like `N001`, so measuring blind fails for reasons that look like circuit faults. |
| `measure(netlist, metrics, …)` | `gain_db`, `bandwidth_hz`, `vout_dc`. |
| `verify_spec(netlist, metrics, …)` | Measure and score against `{name, target, tol}` targets. |
| `audit_netlist(netlist, part)` | Is the named part genuinely instantiated, or replaced by a local subcircuit? |
| `submit(...)` | Acceptance gate. Hash-pinned record of the decision. |

## Layering

`tools.py` holds the implementations and has **no MCP dependency**. `server.py`
is a thin protocol wrapper. `bench/` imports `tools.py` directly for batch runs,
so an interactive session and a measured experiment exercise identical code — a
demo that drifts from the harness measures nothing.

## Security

These tools run a local binary over text supplied by a model, so the netlist is
untrusted input. `.lib` / `.include` directives are restricted to bare
filenames, which LTspice resolves against its own library tree. Absolute paths,
`..` traversal, and embedded separators are rejected before the simulator is
invoked. Simulations are capped by `timeout_s` and run in a temporary directory
that is discarded afterwards.

## Verified behaviour

- Security boundary rejects absolute paths, traversal and separators; allows bare filenames.
- `find_template` returns the correct per-part directive.
- `audit_netlist` separates a fabricated stub (`not_qualified`) from the corpus reference (`qualified`).
- `simulate` surfaces `Fatal Error: Port(pin) count mismatch …` verbatim.
- `list_traces` returns 17 traces for the AD8092 reference, including `V(n001)`.
- `submit` rejects a stub measuring 13.977 dB and accepts a genuine part measuring 13.969 dB.
- MCP handshake, `tools/list` (7 tools) and `tools/call` all succeed over stdio.

Note results arrive in `content[0].text` as a JSON string.
