"""The build must fail when a qualified pass loses its re-verification record."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconcile import (library_drift, missing_reverification,  # noqa: E402
                       no_completion_cause)
from reverify import deletion_outcome  # noqa: E402

K1, K2 = ("A4", "m", 1, "library"), ("B1", "m", 2, "library")


def test_complete_ledger_passes():
    ledger = {K1: {"control": "met"}, K2: {"control": "met"}}
    assert missing_reverification([K1, K2], ledger) == []


def test_deleted_row_is_reported():
    assert missing_reverification([K1, K2], {K1: {"control": "met"}}) == [K2]


def test_failed_control_is_reported():
    ledger = {K1: {"control": "met"}, K2: {"control": "no_sim"}}
    assert missing_reverification([K1, K2], ledger) == [K2]


def test_deletion_outcomes_are_explicit():
    assert deletion_outcome("lost") == "finite_miss"
    assert deletion_outcome("zero_output") == "zero_output"
    assert deletion_outcome("met") == "still_met"


def test_no_completion_causes():
    assert no_completion_cause({"error": "finish_reason='content_filter'"}) == "content_filter"
    assert no_completion_cause({"error": "HTTP 402 Payment Required"}) == "billing"
    assert no_completion_cause({"error": "Read timed out."}) == "network"


def test_missing_or_changed_library_is_reported(tmp_path):
    good, bad = tmp_path / "good.lib", tmp_path / "bad.lib"
    good.write_text(".subckt A 1 2\nR1 1 2 1k\n.ends\n")
    bad.write_text(".subckt B 1 2\nR1 1 2 1k\n.ends\n")
    digest = hashlib.sha256(good.read_bytes()).hexdigest()
    snap = {f"<LTspice>/lib/sub/{n}": digest
            for n in ("good.lib", "bad.lib", "gone.lib")}

    def find(name):
        return str(tmp_path / name) if (tmp_path / name).is_file() else None

    assert library_drift(snap, find=find) == ["<LTspice>/lib/sub/bad.lib",
                                              "<LTspice>/lib/sub/gone.lib"]
