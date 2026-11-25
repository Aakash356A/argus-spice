# Data layout

- `templates/` holds the 59 reference netlists used by the 20 cases (19 distinct
  parts; A4 and D5 share one) and the 40 frozen held-out parts. They are exports
  of example schematics that ship with LTspice and remain subject to the
  vendor's terms.
- `templates_manifest.json` lists the full local export (4,032 files) with each
  file's source example and SHA-256.
- `heldout_parts.json` is the frozen held-out part list. `mutants.py` reads it.
- `library_snapshot.json` pins the vendor library files that the corpus and the
  full template export resolve to.

Every published result recomputes from these files. Two things need the full
export, which a reader obtains from their own LTspice installation: choosing a
new held-out list, and regenerating `library_snapshot.json`. With the published
subset, template lookup in the GUI and the MCP server covers only these parts.
