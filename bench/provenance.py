"""Which definition does a netlist actually bind the requested part name to?

`detect.substituted` answers a weaker question -- is a same-named `.subckt`
defined in this file -- and a reviewer is right that observing a `.lib`
directive plus a matching instance name does not by itself establish which
definition LTspice resolved. Duplicate definitions, local shadowing, wrappers,
nested includes and modified library files all sit between "a directive is
present" and "the manufacturer's model supplied the behaviour".

This module resolves that binding explicitly. It expands `.lib`/`.include`
transitively through the same file-search LTspice is given by
`app.simulation_runner.find_model` (so it resolves what the simulator
resolved, not what the text says), collects every definition of the part
reachable from the netlist, and reports which one wins under SPICE scoping
together with the SHA-256 of the file it came from.

Resolution rule, stated so it can be disagreed with. A `.subckt` defined in
the candidate file shadows any library definition of the *same name*, so an
instance of that name binds locally. Provenance is therefore not a property
of the top-level instance alone but of the elaborated hierarchy: the vendor
model is in the circuit if some instance *anywhere* in the hierarchy names
the part under a name that is not locally defined and that a resolved library
file declares. That distinction is what separates two edits that look alike
in the text -- replacing the part with a local stub of the same name (the
vendor model is gone) and wrapping the genuine part in a local pass-through
(the vendor model is still there, one level down). We resolve reachability
explicitly rather than treating any local definition as disqualifying. No
simulation is run; every output is a property of files on disk.

Encrypted models are the reason this cannot be a pure text check. Most vendor
`.sub` files in this ecosystem ship as encrypted binaries (353 of the first
400 in the installed LTspice `sub/` directory), so no reader -- ours or a
sceptic's -- can grep them for a `.subckt` line. For those files the evidence
we can produce is exactly: the directive resolved to *this* file, whose
SHA-256 is *this*, and whose name declares *this* part. We record that as an
opaque vendor binding and mark it `opaque` in the evidence record, rather
than claiming to have read a definition we cannot read.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from detect import (_same_part, _significant_lines, _subckt_bodies,  # noqa: E402
                    defined_subckt_names, instantiated_subckts)

try:
    from app.simulation_runner import find_model
except Exception:                                          # noqa: BLE001
    def find_model(fullname):                              # type: ignore[misc]
        return None


MAX_DEPTH = 8

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

# `.lib <path>` / `.include <path>` / `.inc <path>`, quoted or bare.
_INCLUDE_RE = re.compile(
    r'^\s*\.(?:lib|inc(?:lude)?)\s+(?P<path>"[^"]+"|\S+)', re.IGNORECASE)

# Libraries every netlist pulls in; they define no vendor macro-model and
# carry no provenance information about the requested part.
_GENERIC = ("standard.", "cmp/standard")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ResolvedFile:
    directive: str
    requested: str
    path: str | None          # None when the directive does not resolve
    sha256: str | None
    depth: int
    defines_part: bool
    # True when the file is an encrypted vendor binary, so `defines_part` rests
    # on the filename and the hash rather than on a readable `.subckt` line.
    opaque: bool = False


@dataclass
class Resolution:
    """What the part name binds to, and on what evidence."""
    part: str
    instantiated: bool                     # an X line calls the part
    local_definitions: list[str] = field(default_factory=list)
    files: list[ResolvedFile] = field(default_factory=list)
    vendor_files: list[ResolvedFile] = field(default_factory=list)
    unresolved_directives: list[str] = field(default_factory=list)

    @property
    def opaque(self) -> bool:
        """The winning vendor definition is inside an encrypted binary."""
        return bool(self.vendor_files) and all(f.opaque for f in self.vendor_files)

    # Set by resolve(): does any instance in the elaborated hierarchy bind the
    # part to a library definition rather than to a local one?
    vendor_reachable: bool = False
    # The instance line, if any, that carries that binding -- released so a
    # reader can check the claim rather than take it.
    vendor_instance: str | None = None

    @property
    def shadowed(self) -> bool:
        """A local definition and a library definition both claim the name."""
        return bool(self.local_definitions) and bool(self.vendor_files)

    @property
    def duplicate_vendor(self) -> bool:
        """More than one resolved library file defines the part."""
        return len({f.path for f in self.vendor_files}) > 1

    @property
    def binding(self) -> str:
        """local | vendor | ambiguous | unresolved.

        `vendor` requires that a library file declaring the part was resolved
        *and* that some instance in the hierarchy actually binds to it. A
        local definition of the same name is disqualifying only when it is
        the definition the instance binds to.
        """
        if not self.vendor_files:
            return "local" if self.local_definitions else "unresolved"
        if self.duplicate_vendor:
            return "ambiguous"
        if self.vendor_reachable:
            return "vendor"
        return "ambiguous" if self.local_definitions else "unresolved"

    @property
    def vendor_supplied(self) -> bool:
        """The strict predicate: the part is instantiated somewhere in the
        elaborated hierarchy and that instance binds to a vendor definition."""
        return self.instantiated and self.binding == "vendor"

    def evidence(self, anonymise: bool = True) -> dict:
        """The provenance half of the Layer-3 evidence record.

        Resolved paths are normalised by default. An absolute path on the
        machine that produced the record carries the operating user's home
        directory, which would de-anonymise a double-blind submission the
        moment the record is released; the file identity a reader needs is
        carried by the SHA-256 and the library-relative name, not by the
        prefix. Pass `anonymise=False` to keep machine paths for local
        debugging.
        """
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
            "duplicate_vendor_definitions": self.duplicate_vendor,
            "opaque_vendor_model": self.opaque,
            "local_definitions": self.local_definitions,
            "vendor_reachable": self.vendor_reachable,
            "vendor_instance": self.vendor_instance,
            "resolved_model_path": path_of(winner) if winner else None,
            "resolved_model_sha256": winner.sha256 if winner else None,
            "transitive_includes": [
                {"directive": f.directive, "resolved": path_of(f),
                 "sha256": f.sha256, "depth": f.depth,
                 "defines_part": f.defines_part}
                for f in self.files
            ],
            "unresolved_directives": self.unresolved_directives,
        }


def _includes(text: str) -> list[tuple[str, str]]:
    """(whole directive, requested path) for each include-like line."""
    out = []
    for line in _significant_lines(text):
        m = _INCLUDE_RE.match(line)
        if not m:
            continue
        raw = m.group("path")
        token = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        out.append((line, token))
    return out


_ENCRYPTED_MARKER = b"<Binary File>"


def _is_encrypted(blob: bytes) -> bool:
    """LTspice's encrypted-model container announces itself in the first bytes."""
    return _ENCRYPTED_MARKER in blob[:256]


def _file_defines_part(text: str, part: str) -> bool:
    return any(_same_part(name, part) for name in defined_subckt_names(text))


def _declares_part(path: Path, part: str) -> bool:
    """Filename evidence, used only for encrypted models.

    `ADR1399H.sub` declares ADR1399H the same way `LT3045-1.sub` declares
    LT3045-1. This is weaker than reading a `.subckt` line and is never used
    for a readable file; it is recorded as `opaque` wherever it applies.
    """
    return _same_part(path.stem, part)


def _instances_in_hierarchy(netlist: str) -> list[tuple[str, str]]:
    """(instance line, called subcircuit name) at top level and inside every
    locally defined subcircuit body.

    A wrapper hides its call to the genuine part one level down, so scanning
    only top-level X lines cannot tell a pass-through from a replacement.
    """
    out = []
    local_bodies = _subckt_bodies(netlist)
    top = [l for l in _significant_lines(netlist)]
    # `_subckt_bodies` already splits bodies out; top-level lines are whatever
    # is not inside one, but re-deriving that is unnecessary -- scanning both
    # the whole file and each body double-counts nothing that matters here,
    # since we only ever ask whether *some* instance binds to the vendor.
    for line in top:
        if line[:1].upper() == "X":
            tokens = line.split()
            if len(tokens) >= 3:
                out.append((line, tokens[-1]))
    for body in local_bodies.values():
        for line in body:
            if line[:1].upper() == "X":
                tokens = line.split()
                if len(tokens) >= 3:
                    out.append((line, tokens[-1]))
    return out


def resolve(netlist: str, part: str, max_depth: int = MAX_DEPTH) -> Resolution:
    """Resolve `part` against `netlist` and everything it transitively includes.

    Include order is followed and each resolved file is visited once, so a
    diamond include does not double-count as a duplicate definition.
    """
    local_names = defined_subckt_names(netlist)
    hierarchy = _instances_in_hierarchy(netlist)
    res = Resolution(
        part=part,
        instantiated=any(_same_part(n, part) for _, n in hierarchy),
        local_definitions=[n for n in local_names if _same_part(n, part)],
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
            res.files.append(ResolvedFile(directive, token, None, None, depth, False))
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
        encrypted = _is_encrypted(blob)
        text = "" if encrypted else blob.decode("utf-8", errors="ignore")
        defines = (_declares_part(path, part) if encrypted
                   else _file_defines_part(text, part))
        entry = ResolvedFile(directive, token, resolved, _sha256(path), depth,
                             defines, opaque=encrypted)
        res.files.append(entry)
        if defines:
            res.vendor_files.append(entry)
        # An encrypted container exposes no further directives to follow.
        for d2, t2 in _includes(text):
            frontier.append((d2, t2, depth + 1))

    # Reachability: an instance binds to the vendor definition when the name it
    # calls is stem-equal to the part and is *not* defined locally under that
    # exact name. `_same_part` is stem-based, so a local `AD8237_WRAP` is
    # stem-equal to AD8237 without shadowing it -- the exact-name comparison
    # here is what keeps the two apart.
    local_exact = {n.strip().upper() for n in local_names}
    if res.vendor_files:
        for line, called in hierarchy:
            if _same_part(called, part) and called.strip().upper() not in local_exact:
                res.vendor_reachable = True
                res.vendor_instance = line
                break

    return res


def vendor_supplied(netlist: str, part: str) -> bool:
    """Boolean form, for use as an audit predicate."""
    return resolve(netlist, part).vendor_supplied
