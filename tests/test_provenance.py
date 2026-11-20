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


@pytest.fixture
def lib(tmp_path, monkeypatch):
    files = {}

    def add(name, text):
        p = tmp_path / name
        p.write_text(text)
        files[name] = str(p)

    monkeypatch.setattr(provenance, "find_model", lambda tok: files.get(Path(tok).name))
    return add


def r(netlist, part=PART):
    return resolve(netlist, part, snapshot={})


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


def test_digest_mismatch_is_untrusted(lib, tmp_path):
    lib("LT3045-1.sub", VENDOR)
    net = f".lib LT3045-1.sub\nXU1 IN OUT SET 0 IN {PART}\n.end\n"
    key = provenance._normalise_path(str(tmp_path / "LT3045-1.sub"))
    res = resolve(net, PART, snapshot={key: "0" * 64})
    assert res.binding == "untrusted" and not res.vendor_supplied
