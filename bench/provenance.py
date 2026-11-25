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
4. Includes. The deck is first put through the simulation runner's own include
   rewriting (`app.simulation_runner.replace_lib_paths`) and title-line rule, so
   top-level directives name the files the runner hands to LTspice. Nested
   directives are not rewritten by the runner; LTspice 17.2.4 takes an absolute
   nested path as written and a relative one from the including file's
   directory first, and those are the only two forms followed. Each file is
   parsed once, but every include edge is kept. If any directive does not
   resolve under these rules the binding is `unresolved`: an unread file could
   define anything.
5. Shadowing. A local `.subckt` with the requested name disqualifies the
   binding whether or not the simulator would prefer it or report an
   ambiguity, because in neither case is the vendor model established.
6. Counting. Vendor definitions are counted per definition, not per file, so
   two `.subckt` blocks of the name in one file are a duplicate. The rule also
   applies to the subcircuits the model calls: if one of them is defined more
   than once across the deck and its libraries, the binding is `ambiguous`.
7. Opaque files. Encrypted vendor binaries cannot be read; for those the
   evidence is the filename stem (exact match to the requested name) and the
   file's SHA-256. Such bindings are marked `opaque`.
8. Trust root. Every file in the binding's include closure -- the file that
   defines the part and every file it pulls in -- must be listed in
   `data/library_snapshot.json` with a matching SHA-256. A file absent from
   the snapshot, or whose digest differs, makes the binding `untrusted`. The
   closure follows include edges only: a subcircuit the model calls but that no
   file in its closure defines, so that the deck or a sibling library supplies
   it, is not digest-checked. No readable definition in the pinned snapshot
   calls one (`reconcile.outside_helpers`).
9. End of deck. Text after `.end` is ignored, as LTspice ignores it.
10. Library sections. `.lib <file> <section>` loading and libraries that
   contain `.endl` (with or without a name) are outside the supported subset
   and make the binding `unsupported`. LTspice 17.2.4 on macOS does load
   sections in at least one syntax (`.lib 'file' NAME`, single quotes), so this
   is a restriction of the resolver, not a property of the simulator.

No simulation is run; every output is a property of files on disk. This is a
static approximation of what the simulator elaborates. It does not model
`.param`-dependent subcircuit selection, and it checks subcircuit names only:
`.model`, `.param` and `.func` names that a deck shares with a vendor model,
and parameters overridden on the instance line, are not compared.
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

from app.simulation_runner import (ensure_title_line, find_model,  # noqa: E402
                                   replace_lib_paths)


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
    r"""^\s*\.(?:lib|inc(?:lude)?)\s*(?P<path>"[^"]+"|'[^']+'|\S+)""",
    re.IGNORECASE)

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
        if re.match(r"(?i)^\.end\s*$", line):
            break                      # LTspice ignores everything after .end
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
    top, bodies, defs = _hierarchy(text)
    return top, bodies, [name for name, _ in defs]


def _hierarchy(text: str):
    """As `split_hierarchy`, with each definition's enclosing subcircuit."""
    top: list[str] = []
    bodies: dict[str, list[str]] = {}
    defs: list[tuple[str, str | None]] = []
    stack: list[str] = []
    for line in logical_lines(text):
        low = line.lower()
        if low.startswith(".subckt"):
            tokens = line.split()
            name = tokens[1] if len(tokens) >= 2 else "?"
            defs.append((name, stack[-1] if stack else None))
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
    return top, bodies, defs


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


def has_sections(text: str) -> bool:
    """Does the text contain `.lib ... .endl` sections (named or bare)?"""
    return any(re.match(r"(?i)^\.endl\b", l) for l in logical_lines(text))


def _includes(text: str) -> list[tuple[str, str, str | None]]:
    """(directive, requested path, requested section) for each include line."""
    out = []
    for line in logical_lines(text):
        m = _INCLUDE_RE.match(line)
        if not m:
            continue
        raw = m.group("path")
        token = raw[1:-1] if raw[:1] in "\"'" and raw[-1:] == raw[:1] else raw
        rest = line[m.end():].split()
        section = rest[0] if (line.lower().startswith(".lib") and rest) else None
        out.append((line, token, section))
    return out


def _decode(blob: bytes) -> str:
    """Library text, including the UTF-16 files LTspice itself can read."""
    if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return blob.decode("utf-16", errors="ignore")
    if blob[:512].count(0) > len(blob[:512]) // 4:
        return blob.decode("utf-16-le", errors="ignore")
    return blob.decode("utf-8", errors="ignore")


@dataclass
class _Library:
    """One parsed library file (cached by path, size and modification time)."""
    sha256: str
    encrypted: bool
    sectioned: bool = False
    includes: list = field(default_factory=list)
    bodies: dict = field(default_factory=dict)
    defs: list = field(default_factory=list)


_library_cache: dict[tuple, _Library] = {}


def _load_library(path: Path) -> _Library:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _library_cache:
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if _ENCRYPTED_MARKER in blob[:256]:
            lib = _Library(digest, True)
        else:
            text = _decode(blob)
            _, bodies, defs = _hierarchy(text)
            lib = _Library(digest, False, has_sections(text), _includes(text),
                           bodies, defs)
        _library_cache[key] = lib
    return _library_cache[key]


def effective_includes(netlist: str) -> list[tuple[str, str | None, str, str | None]]:
    """(directive as written, file handed to LTspice or None, token, section).

    The file is what the simulation runner's own rewriting puts in the deck.
    A directive the runner leaves as written has no established target.
    """
    written = _includes(netlist)
    rewritten = _includes(replace_lib_paths(netlist, find=find_model))
    if len(written) != len(rewritten):
        return [(d, None, t, s) for d, t, s in written]
    out = []
    for (d, token, section), (_, target, _) in zip(written, rewritten):
        same = target == token and not Path(target).is_file()
        found = None if same or not Path(target).is_absolute() else target
        if found is not None and not Path(found).is_file():
            found = None
        out.append((d, found, token, section))
    return out


def effective_body(netlist: str) -> str:
    """Deck text LTspice parses: the runner's title rule, minus line 1."""
    return ensure_title_line(netlist).partition("\n")[2]


def _nested_target(token: str, parent: str) -> str | None:
    """File a nested directive loads, under the two forms that are supported."""
    tok = token.replace("\\", "/")
    path = Path(tok) if Path(tok).is_absolute() else Path(parent).parent / tok
    return str(path.resolve()) if path.is_file() else None


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
    parent: str | None = None   # resolved path of the first including file
    section: str | None = None  # requested .lib section, if any

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
    unsupported: list[str] = field(default_factory=list)
    edges: list[tuple[str | None, str]] = field(default_factory=list)
    captured: list[str] = field(default_factory=list)  # helper names defined twice

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

    def closure(self) -> list[ResolvedFile]:
        """Vendor files plus every file they transitively include."""
        keep = {f.path for f in self.vendor_files}
        frontier = list(keep)
        while frontier:
            at = frontier.pop()
            for parent, child in self.edges:
                if parent == at and child not in keep:
                    keep.add(child)
                    frontier.append(child)
        return [f for f in self.files if f.path in keep]

    @property
    def digest_mismatch(self) -> bool:
        """Some file in the include closure is not positively pinned."""
        return any(f.pinned is not True for f in self.closure())

    @property
    def vendor_reachable(self) -> bool:
        return self.instantiated and self.vendor_instance is not None

    @property
    def binding(self) -> str:
        """local | vendor | ambiguous | untrusted | unsupported | unresolved."""
        if self.local_definitions:
            return "local"
        if self.unsupported:
            return "unsupported"
        if not self.vendor_files or self.unresolved_directives:
            return "unresolved"
        if self.duplicate_vendor or self.captured:
            return "ambiguous"
        if self.digest_mismatch:
            return "untrusted"
        return "vendor"

    @property
    def vendor_supplied(self) -> bool:
        """The strict predicate: a reachable instance calls the requested name,
        no local definition claims it, every include resolves, exactly one
        vendor definition is reachable, no subcircuit it calls is defined
        twice, and every file in its include closure matches the snapshot."""
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
                 "sha256": f.sha256, "depth": f.depth, "section": f.section,
                 "definitions": f.definitions, "pinned": f.pinned}
                for f in self.files
            ],
            "include_edges": [[_normalise_path(a) if a and anonymise else a,
                               _normalise_path(b) if anonymise else b]
                              for a, b in self.edges],
            "captured_helpers": self.captured,
            "unresolved_directives": self.unresolved_directives,
            "unsupported": self.unsupported,
        }


def resolve(netlist: str, part: str, max_depth: int = MAX_DEPTH,
            snapshot: dict | None = None) -> Resolution:
    """Resolve `part` against `netlist` and everything it transitively includes."""
    want = part.strip().upper()
    snapshot = load_snapshot() if snapshot is None else snapshot
    body = effective_body(netlist)
    _, deck_bodies, deck_defs = _hierarchy(body)
    calls = [(line, c) for line, c in elaborated_instances(body)
             if c.upper() == want]
    res = Resolution(
        part=part,
        instantiated=bool(calls),
        local_definitions=[n for n, _ in deck_defs if n.upper() == want],
    )
    if has_sections(netlist):
        res.unsupported.append("library section inside the deck")

    loaded: dict[str, _Library] = {}
    frontier = [(d, target, tok, sec, 1, None)
                for d, target, tok, sec in effective_includes(netlist)]
    while frontier:
        directive, target, token, section, depth, parent = frontier.pop(0)
        if section is not None:
            res.unsupported.append(f"library section requested: {directive}")
        if target is None:
            res.unresolved_directives.append(directive)
            res.files.append(ResolvedFile(directive, token, None, None, depth, 0,
                                          parent=parent, section=section))
            continue
        path = Path(target).resolve()
        resolved = str(path)
        res.edges.append((parent, resolved))     # every edge, even to a seen file
        if resolved in loaded:
            continue
        if depth > max_depth:
            res.unsupported.append(f"include depth exceeds {max_depth}: {directive}")
            continue
        try:
            lib = _load_library(path)
        except OSError:
            res.unresolved_directives.append(directive)
            continue
        loaded[resolved] = lib
        if lib.sectioned:
            res.unsupported.append(f"sectioned library: {directive}")
        if lib.encrypted:
            defs = 1 if path.stem.strip().upper() == want else 0
        else:
            defs = sum(1 for n, _ in lib.defs if n.upper() == want)
        rel = _normalise_path(resolved)
        pinned = (snapshot[rel] == lib.sha256) if rel in snapshot else None
        entry = ResolvedFile(directive, token, resolved, lib.sha256, depth, defs,
                             opaque=lib.encrypted, pinned=pinned, parent=parent,
                             section=section)
        res.files.append(entry)
        if defs:
            res.vendor_files.append(entry)
        for d2, t2, s2 in lib.includes:
            frontier.append((d2, _nested_target(t2, resolved), t2, s2,
                             depth + 1, resolved))

    if len(res.vendor_files) == 1 and not res.vendor_files[0].opaque:
        res.captured = _captured(want, deck_bodies, deck_defs, loaded,
                                 res.vendor_files[0].path)
    if res.vendor_files and not res.local_definitions and calls:
        res.vendor_instance = calls[0][0]
    return res


def _captured(want, deck_bodies, deck_defs, loaded, vendor_path) -> list[str]:
    """Subcircuits the vendor model calls, directly or not, whose name has more
    than one global definition across the deck and every loaded library."""
    count: dict[str, int] = {}
    for defs in [deck_defs] + [lib.defs for lib in loaded.values()]:
        for name, enclosing in defs:
            if enclosing is None:
                count[name.upper()] = count.get(name.upper(), 0) + 1
    home = loaded[vendor_path]
    twice, queue, seen = [], [(want, home)], {want}
    while queue:
        name, lib = queue.pop()
        nested = {n.upper() for n, enclosing in lib.defs if enclosing == name}
        for line in lib.bodies.get(name, []):
            callee = callee_of(line)
            if callee is None or callee.upper() in seen:
                continue
            key = callee.upper()
            seen.add(key)
            if key not in nested and count.get(key, 0) > 1:
                twice.append(callee)
            owner = lib if key in lib.bodies else next(
                (l for l in loaded.values() if key in l.bodies), None)
            if owner is not None:
                queue.append((key, owner))
    return twice


def vendor_supplied(netlist: str, part: str) -> bool:
    """Boolean form, for use as an audit predicate."""
    return resolve(netlist, part).vendor_supplied
