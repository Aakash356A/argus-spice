"""Resolver fixtures: each case is one way a static binding check can be wrong.

The library is a plaintext fixture in a temp directory and `find_model` is
pointed at it, so these tests need no LTspice installation.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import provenance  # noqa: E402
from provenance import resolve  # noqa: E402

PART = "LT3045-1"
VENDOR = f""".subckt {PART} IN OUT SET GND EN
R1 IN OUT 1
.ends {PART}
"""


FILES: dict[str, str] = {}


@pytest.fixture
def lib(tmp_path, monkeypatch):
    FILES.clear()

    def add(name, text):
        p = tmp_path / name
        p.write_text(text)
        FILES[name] = str(p)

    monkeypatch.setattr(provenance, "find_model", lambda tok: FILES.get(Path(tok).name))
    return add


def pinned(exclude=(), corrupt=()):
    """A snapshot pinning every fixture file, optionally leaving some out or
    recording a wrong digest for them."""
    snap = {}
    for name, path in FILES.items():
        if name in exclude:
            continue
        digest = provenance._sha256(Path(path))
        snap[provenance._normalise_path(path)] = "0" * 64 if name in corrupt else digest
    return snap


def r(netlist, part=PART, **kw):
    return resolve(netlist, part, snapshot=pinned(**kw))


def test_genuine_binds(lib):
    lib("LT3045-1.sub", VENDOR)
    res = r(f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n")
    assert res.vendor_supplied and res.binding == "vendor"


def test_call_inside_uninstantiated_subckt_binds_nothing(lib):
    lib("LT3045-1.sub", VENDOR)
    net = (".lib LT3045-1.sub\n.subckt UNUSED a b c d e\n"
           f"XU1 a b c d e {PART}\n.ends UNUSED\nR1 IN OUT 1\n.end\n")
    assert not r(net).vendor_supplied


def test_wrapper_that_is_called_still_binds(lib):
    lib("LT3045-1.sub", VENDOR)
    net = (".lib LT3045-1.sub\n.subckt WRAP a b c d e\n"
           f"XIN a b c d e {PART}\n.ends WRAP\nXW IN OUT SET 0 IN WRAP\n.end\n")
    assert r(net).vendor_supplied


def test_two_definitions_in_one_file_are_ambiguous(lib):
    lib("LT3045-1.sub", VENDOR + VENDOR)
    res = r(f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n")
    assert res.binding == "ambiguous" and not res.vendor_supplied


def test_base_part_does_not_bind_suffixed_request(lib):
    lib("LT3045.sub", VENDOR.replace(PART, "LT3045"))
    res = r(".lib LT3045.sub\nXU1 IN OUT SET 0 IN LT3045\n.end\n")
    assert not res.vendor_supplied


def test_trailing_parameters_are_not_the_callee(lib):
    lib("LT3045-1.sub", VENDOR)
    for tail in ("R=1k", "params: R=1k G=2"):
        res = r(f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART} {tail}\n.end\n")
        assert res.vendor_supplied, tail


def test_continuation_line_carries_the_callee(lib):
    lib("LT3045-1.sub", VENDOR)
    res = r(f".lib LT3045-1.sub\nXU1 IN OUT\n+ SET 0 IN {PART}\n.end\n")
    assert res.vendor_supplied


def test_local_shadow_disqualifies(lib):
    lib("LT3045-1.sub", VENDOR)
    net = (f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n"
           f".subckt {PART} a b c d e\nR1 a b 1\n.ends {PART}\n.end\n")
    assert r(net).binding == "local"


def test_wrong_file_does_not_bind(lib):
    lib("OTHER.sub", VENDOR.replace(PART, "LT1761"))
    res = r(f".lib OTHER.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n")
    assert res.binding == "unresolved"


def test_digest_mismatch_is_untrusted(lib):
    lib("LT3045-1.sub", VENDOR)
    net = f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n"
    res = r(net, corrupt=("LT3045-1.sub",))
    assert res.binding == "untrusted" and not res.vendor_supplied


# ---- regression tests for the trust-boundary review ----------------------
def test_file_absent_from_snapshot_is_untrusted(lib):
    lib("LT3045-1.sub", VENDOR)
    res = r(f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n",
            exclude=("LT3045-1.sub",))
    assert res.binding == "untrusted" and not res.vendor_supplied


def test_instance_after_end_is_ignored(lib):
    lib("LT3045-1.sub", VENDOR)
    res = r(f".lib LT3045-1.sub\nR9 IN 0 1\n.end\nXU1 IN OUT SET 0 IN {PART}\n")
    assert not res.instantiated and not res.vendor_supplied


def test_section_request_is_unsupported(lib):
    """Sectioned loads are outside the supported subset, in every quoting style
    (LTspice 17.2.4 does load a section from a single-quoted file name)."""
    lib("S.lib", ".lib typ\n.subckt OTHER a b\nR a b 1\n.ends OTHER\n.endl typ\n"
                 ".lib slow\n" + VENDOR + ".endl slow\n")
    for sec in ("typ", "slow"):
        for tok in ("S.lib", "'S.lib'", '"S.lib"'):
            res = r(f".lib {tok} {sec}\nXU1 IN OUT SET 0 IN {PART}\n.end\n")
            assert res.binding == "unsupported" and not res.vendor_supplied


def test_sectioned_library_without_section_is_unsupported(lib):
    lib("S.lib", ".lib slow\n" + VENDOR + ".endl slow\n")
    res = r(f".lib S.lib\nXU1 IN OUT SET 0 IN {PART}\n.end\n")
    assert res.binding == "unsupported" and not res.vendor_supplied


def test_helper_with_mismatched_digest_is_untrusted(lib):
    lib("HELP.sub", ".subckt HELP a b\nR a b 1\n.ends HELP\n")
    lib("LT3045-1.sub", ".inc HELP.sub\n" + VENDOR)
    net = f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n"
    assert r(net).vendor_supplied
    res = r(net, corrupt=("HELP.sub",))
    assert res.binding == "untrusted" and not res.vendor_supplied


# ---- regression tests for include edges, unresolved files and bare .endl ---
HELP = ".subckt HELP a b\nR a b 1\n.ends HELP\n"
CALLS_HELP = VENDOR.replace("R1 IN OUT 1", "X1 IN OUT HELP")
DECK = f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n"


def test_helper_included_directly_and_transitively_stays_in_the_closure(lib):
    lib("HELP.sub", HELP)
    lib("LT3045-1.sub", ".inc HELP.sub\n" + VENDOR)
    for net in (".inc HELP.sub\n" + DECK, DECK.replace(".end", ".inc HELP.sub\n.end")):
        assert r(net).vendor_supplied
        res = r(net, corrupt=("HELP.sub",))
        assert res.binding == "untrusted" and not res.vendor_supplied


def test_include_cycle_terminates(lib):
    lib("HELP.sub", ".inc LT3045-1.sub\n" + HELP)
    lib("LT3045-1.sub", ".inc HELP.sub\n" + VENDOR)
    assert r(DECK).vendor_supplied
    assert r(DECK, corrupt=("HELP.sub",)).binding == "untrusted"


def test_unresolved_nested_include_does_not_qualify(lib):
    for target in ("MISSING.sub", "/nowhere/abs/MISSING.sub"):
        lib("LT3045-1.sub", f'.inc "{target}"\n' + VENDOR)
        res = r(DECK)
        assert res.binding == "unresolved" and not res.vendor_supplied


def test_unresolved_top_level_include_does_not_qualify(lib):
    lib("LT3045-1.sub", VENDOR)
    assert r(".lib NOT_INSTALLED.sub\n" + DECK).binding == "unresolved"


def test_nested_absolute_path_is_followed_as_written(lib, tmp_path):
    """An approved basename at another absolute path is not the approved file:
    LTspice opens the path as written, so its bytes must be the pinned ones."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "HELP.sub").write_text(HELP.replace("R a b 1", "R a b 2"))
    lib("HELP.sub", HELP)
    lib("LT3045-1.sub", f'.inc "{other / "HELP.sub"}"\n' + CALLS_HELP)
    assert not r(DECK).vendor_supplied


def test_bare_endl_is_a_section(lib):
    lib("S.lib", ".lib slow\n" + VENDOR + ".endl\n")
    res = r(f".lib S.lib\nXU1 IN OUT SET 0 IN {PART}\n.end\n")
    assert res.binding == "unsupported" and not res.vendor_supplied


def test_captured_helper_is_ambiguous(lib):
    """The requested name is defined once, but a subcircuit it calls is also
    defined in the deck or in another library."""
    lib("LT3045-1.sub", CALLS_HELP + HELP)
    lib("BOARD.lib", HELP.replace("R a b 1", "R a b 2"))
    assert r(DECK).vendor_supplied
    assert r(".lib BOARD.lib\n" + DECK).binding == "ambiguous"
    assert r(HELP + DECK).binding == "ambiguous"


def test_definition_in_a_utf16_include_is_seen(lib, tmp_path):
    lib("LT3045-1.sub", VENDOR)
    lib("BOARD.lib", "")
    (tmp_path / "BOARD.lib").write_bytes(VENDOR.encode("utf-16"))
    assert r(".lib BOARD.lib\n" + DECK).binding == "ambiguous"


def test_file_named_like_a_standard_library_is_read(lib):
    lib("LT3045-1.sub", VENDOR)
    lib("standard.lib", VENDOR)
    assert r(".lib standard.lib\n" + DECK).binding == "ambiguous"


def test_title_line_is_not_a_circuit_element(lib):
    lib("LT3045-1.sub", VENDOR)
    res = r(f"XU9 a b c d e {PART}\n.lib LT3045-1.sub\nR1 IN OUT 1\n.end\n")
    assert not res.instantiated and not res.vendor_supplied
