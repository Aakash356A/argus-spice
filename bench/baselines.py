"""Structural and digest baselines that ARGUS is compared with.

All of them share one notion of approval -- membership in the pinned library
snapshot -- and all require a reachable instance of the exact requested name
and no local definition of it. They differ in what they ask of the files:

  name_include   some top-level include has an approved file name
  strict_names   every top-level include has an approved file name
  direct_digest  every file the deck includes directly has an approved digest,
                 looked up through the same include rewriting as the runner

None of them opens a file to see what it defines, or follows nested includes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import provenance

BASELINES = ("name_include", "strict_names", "direct_digest")


def _named(deck: str, part: str) -> bool:
    body = provenance.effective_body(deck)
    _, _, names = provenance.split_hierarchy(body)
    want = part.upper()
    return (any(c.upper() == want for _, c in provenance.elaborated_instances(body))
            and want not in {n.upper() for n in names})


def _stem(token: str) -> str:
    return Path(token.replace("\\", "/")).name.upper()


def verdicts(deck: str, part: str, snapshot: dict) -> dict[str, bool]:
    named = _named(deck, part)
    includes = provenance.effective_includes(deck)
    approved = {Path(rel).name.upper() for rel in snapshot}
    names = [_stem(token) for _, _, token, _ in includes]

    def pinned(target):
        if target is None:
            return False
        rel = provenance._normalise_path(str(Path(target).resolve()))
        return snapshot.get(rel) == hashlib.sha256(Path(target).read_bytes()).hexdigest()

    return {
        "name_include": named and any(n in approved for n in names),
        "strict_names": named and bool(names) and all(n in approved for n in names),
        "direct_digest": (named and bool(includes)
                          and all(pinned(t) for _, t, _, _ in includes)),
    }
