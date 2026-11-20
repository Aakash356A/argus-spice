"""Which definition does a netlist actually bind the requested part name to?

`detect.substituted` answers a weaker question -- is a same-named `.subckt`
defined in this file -- and observing a `.lib` directive plus a matching
instance name does not by itself establish which definition the simulator
used. Duplicate definitions, local shadowing, wrappers, nested includes and
modified library files all sit between "a directive is present" and "the
manufacturer's model is in the circuit".

This module resolves that binding statically, under rules stated here so they
can be disagreed with:

1. Parsing. Lines are joined across SPICE `+` continuations, whole-line `*`
   comments and trailing `;` comments are dropped. On an `X` line the callee
   is the last token that is neither a `name=value` parameter nor follows a
   `params:` keyword.
2. Elaboration. Traversal starts at the *top-level* `X` instances and descends
   only into locally defined subcircuits that are actually called, so an
   instance inside a subcircuit nobody instantiates binds nothing.
3. Names. Binding is by exact subcircuit name, case-insensitive (SPICE names
   are). `LT3045` does not bind a request for `LT3045-1`. Variant-name
   matching belongs to the substitution *diagnostics* in `detect.py`, never to
   this predicate.
4. Includes. `.lib`/`.include` are expanded transitively (depth <= 8) through
   the same basename lookup the simulation runner uses
   (`app.simulation_runner.find_model`); each resolved file is visited once.
5. Shadowing. A local `.subckt` with the requested name disqualifies the
   binding whether or not the simulator would prefer it or report an
   ambiguity, because in neither case is the vendor model established.
6. Counting. Vendor definitions are counted per definition, not per file, so
   two `.subckt` blocks of the name in one file are a duplicate.
7. Opaque files. Encrypted vendor binaries cannot be read; for those the
   evidence is the filename stem (exact match to the requested name) and the
   file's SHA-256. Such bindings are marked `opaque`.
8. Trust root. When `data/library_snapshot.json` lists a resolved file, its
   SHA-256 must match the pinned digest; a mismatch disqualifies the binding.
   A file absent from the snapshot is reported as unpinned, not trusted.

No simulation is run; every output is a property of files on disk. This is a
static approximation of what the simulator elaborates: it does not model
`.param`-dependent subcircuit selection, `.lib <file> <section>` sections, or
simulator-specific search-path precedence beyond the basename lookup.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from app.simulation_runner import find_model
except Exception:                                          # noqa: BLE001
    def find_model(fullname):                              # type: ignore[misc]
        return None


MAX_DEPTH = 8
SNAPSHOT_PATH = (Path(__file__).resolve().parent.parent / "data"
                 / "library_snapshot.json")

# Directory names that mark the start of the portable part of a library path.
# Everything to their left is machine- and user-specific.
_PATH_ROOTS = ("lib/sub/", "lib/cmp/", "lib/sym/", "lib/", "templates/")


def _normalise_path(path: str) -> str:
    """Strip the machine-specific prefix from a resolved library path."""
    unix = path.replace("\\", "/")
    for root in _PATH_ROOTS:
        idx = unix.rfind(root)
        if idx != -1:
            return "<LTspice>/" + unix[idx:]
    return "<resolved>/" + unix.rsplit("/", 1)[-1]


_INCLUDE_RE = re.compile(
    r'^\s*\.(?:lib|inc(?:lude)?)\s+(?P<path>"[^"]+"|\S+)', re.IGNORECASE)

# Libraries every netlist pulls in; they define no vendor macro-model.
_GENERIC = ("standard.", "cmp/standard")

_ENCRYPTED_MARKER = b"<Binary File>"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_snapshot_cache: dict | None = None


def load_snapshot() -> dict:
    """{library-relative path: sha256} for the pinned library snapshot."""
    global _snapshot_cache
    if _snapshot_cache is None:
        try:
            _snapshot_cache = json.loads(SNAPSHOT_PATH.read_text())["files"]
        except (OSError, ValueError, KeyError):
            _snapshot_cache = {}
    return _snapshot_cache


# ----------------------------------------------------------------- parsing
def logical_lines(text: str) -> list[str]:
    """Significant lines with `+` continuations joined and comments removed."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("*"):
            continue
        if line.startswith("+"):
            if out:
                out[-1] = out[-1] + " " + line[1:].strip()
            continue
        out.append(line)
    return [" ".join(l.split()) for l in out]


def callee_of(line: str) -> str | None:
    """Subcircuit called by an X line, skipping trailing parameters."""
    tokens = line.split()
    if len(tokens) < 2 or tokens[0][:1].upper() != "X":
        return None
    kept = []
    for tok in tokens[1:]:
        if tok.lower() in ("params:", "param:"):
            break
        if "=" in tok:
            break
        kept.append(tok)
    return kept[-1] if kept else None


def split_hierarchy(text: str) -> tuple[list[str], dict[str, list[str]],
                                         list[str]]:
    """(top-level lines, {UPPER name: body lines}, local names as written)."""
    top: list[str] = []
    bodies: dict[str, list[str]] = {}
    names: list[str] = []
    stack: list[str] = []
    for line in logical_lines(text):
        low = line.lower()
        if low.startswith(".subckt"):
            tokens = line.split()
            name = tokens[1] if len(tokens) >= 2 else "?"
            names.append(name)
            bodies.setdefault(name.upper(), [])
            stack.append(name.upper())
            continue
        if low.startswith(".ends"):
            if stack:
                stack.pop()
            continue
        if stack:
            bodies[stack[-1]].append(line)
        else:
            top.append(line)
    return top, bodies, names


def elaborated_instances(text: str) -> list[tuple[str, str]]:
    """(instance line, callee) reachable from the top level."""
    top, bodies, _ = split_hierarchy(text)
    out: list[tuple[str, str]] = []
    queue = [l for l in top if l[:1].upper() == "X"]
    visited: set[str] = set()
    while queue:
        line = queue.pop(0)
        callee = callee_of(line)
        if callee is None:
            continue
        out.append((line, callee))
        key = callee.upper()
        if key in bodies and key not in visited:
            visited.add(key)
            queue.extend(l for l in bodies[key] if l[:1].upper() == "X")
    return out


def _subckt_names(text: str) -> list[str]:
    return [l.split()[1] for l in logical_lines(text)
            if l.lower().startswith(".subckt") and len(l.split()) >= 2]


def _includes(text: str) -> list[tuple[str, str]]:
    out = []
    for line in logical_lines(text):
        m = _INCLUDE_RE.match(line)
        if not m:
            continue
        raw = m.group("path")
        token = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        out.append((line, token))
    return out


# ---------------------------------------------------------------- results
@dataclass
class ResolvedFile:
    directive: str
    requested: str
    path: str | None          # None when the directive does not resolve
    sha256: str | None
    depth: int
    definitions: int          # .subckt blocks of the requested name in it
    opaque: bool = False
    pinned: bool | None = None  # digest matches snapshot / mismatch / unlisted

    @property
    def defines_part(self) -> bool:
        return self.definitions > 0


@dataclass
class Resolution:
    """What the part name binds to, and on what evidence."""
    part: str
    instantiated: bool                       # a reachable X line calls it
    local_definitions: list[str] = field(default_factory=list)
    files: list[ResolvedFile] = field(default_factory=list)
    vendor_files: list[ResolvedFile] = field(default_factory=list)
    unresolved_directives: list[str] = field(default_factory=list)
    vendor_instance: str | None = None

    @property
    def vendor_definitions(self) -> int:
        return sum(f.definitions for f in self.vendor_files)

    @property
    def opaque(self) -> bool:
        return bool(self.vendor_files) and all(f.opaque for f in self.vendor_files)

    @property
    def shadowed(self) -> bool:
        return bool(self.local_definitions) and bool(self.vendor_files)

    @property
    def duplicate_vendor(self) -> bool:
        return self.vendor_definitions > 1

    @property
    def digest_mismatch(self) -> bool:
        return any(f.pinned is False for f in self.vendor_files)

    @property
    def vendor_reachable(self) -> bool:
        return self.instantiated and self.vendor_instance is not None

    @property
    def binding(self) -> str:
        """local | vendor | ambiguous | untrusted | unresolved."""
        if self.local_definitions:
            return "local"
        if not self.vendor_files:
            return "unresolved"
        if self.duplicate_vendor:
            return "ambiguous"
        if self.digest_mismatch:
            return "untrusted"
        return "vendor"

    @property
    def vendor_supplied(self) -> bool:
        """The strict predicate: a reachable instance calls the requested name,
        no local definition claims it, and exactly one vendor definition is
        reachable through the includes (with a matching pinned digest where
        the snapshot lists the file)."""
        return self.instantiated and self.binding == "vendor"

    def evidence(self, anonymise: bool = True) -> dict:
        def path_of(f):
            if f.path is None:
                return None
            return _normalise_path(f.path) if anonymise else f.path

        winner = self.vendor_files[0] if self.binding == "vendor" else None
        return {
            "part": self.part,
            "instantiated": self.instantiated,
            "binding": self.binding,
            "vendor_supplied": self.vendor_supplied,
            "shadowed": self.shadowed,
            "vendor_definitions": self.vendor_definitions,
            "opaque_vendor_model": self.opaque,
            "local_definitions": self.local_definitions,
            "vendor_instance": self.vendor_instance,
            "resolved_model_path": path_of(winner) if winner else None,
            "resolved_model_sha256": winner.sha256 if winner else None,
            "snapshot_pinned": winner.pinned if winner else None,
            "transitive_includes": [
                {"directive": f.directive, "resolved": path_of(f),
                 "sha256": f.sha256, "depth": f.depth,
                 "definitions": f.definitions, "pinned": f.pinned}
                for f in self.files
            ],
            "unresolved_directives": self.unresolved_directives,
        }


def resolve(netlist: str, part: str, max_depth: int = MAX_DEPTH,
            snapshot: dict | None = None) -> Resolution:
    """Resolve `part` against `netlist` and everything it transitively includes."""
    want = part.strip().upper()
    snapshot = load_snapshot() if snapshot is None else snapshot
    _, _, local_names = split_hierarchy(netlist)
    reachable = elaborated_instances(netlist)
    calls = [(line, c) for line, c in reachable if c.upper() == want]
    res = Resolution(
        part=part,
        instantiated=bool(calls),
        local_definitions=[n for n in local_names if n.upper() == want],
    )

    seen: set[str] = set()
    frontier = [(d, t, 1) for d, t in _includes(netlist)]
    while frontier:
        directive, token, depth = frontier.pop(0)
        if depth > max_depth:
            continue
        if any(g in token.lower() for g in _GENERIC):
            continue
        resolved = find_model(token)
        if not resolved:
            res.unresolved_directives.append(directive)
            res.files.append(ResolvedFile(directive, token, None, None, depth, 0))
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        path = Path(resolved)
        try:
            blob = path.read_bytes()
        except OSError:
            res.unresolved_directives.append(directive)
            continue
        encrypted = _ENCRYPTED_MARKER in blob[:256]
        text = "" if encrypted else blob.decode("utf-8", errors="ignore")
        if encrypted:
            defs = 1 if path.stem.strip().upper() == want else 0
        else:
            defs = sum(1 for n in _subckt_names(text) if n.upper() == want)
        digest = _sha256(path)
        rel = _normalise_path(resolved)
        pinned = (snapshot[rel] == digest) if rel in snapshot else None
        entry = ResolvedFile(directive, token, resolved, digest, depth, defs,
                             opaque=encrypted, pinned=pinned)
        res.files.append(entry)
        if defs:
            res.vendor_files.append(entry)
        for d2, t2 in _includes(text):
            frontier.append((d2, t2, depth + 1))

    if res.vendor_files and not res.local_definitions and calls:
        res.vendor_instance = calls[0][0]
    return res


def vendor_supplied(netlist: str, part: str) -> bool:
    """Boolean form, for use as an audit predicate."""
    return resolve(netlist, part).vendor_supplied
