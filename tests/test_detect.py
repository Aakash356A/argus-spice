"""Static checks for role-based pin-order validation."""

import unittest
from pathlib import Path

from bench.detect import Audit, audit, infer_pin_roles, pin_role_mismatch


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "data" / "templates"
NETLISTS = ROOT / "bench" / "runs" / "20260809T211634Z" / "netlists"


class PinRoleMismatchTests(unittest.TestCase):
    def template(self, part: str) -> str:
        return (TEMPLATES / f"{part}.net").read_text(encoding="utf-8")

    def test_infers_corpus_template_roles(self):
        self.assertEqual(
            infer_pin_roles(self.template("AD8092"), "AD8092"),
            ["signal", "signal", "supply", "supply", "signal"],
        )
        self.assertEqual(
            infer_pin_roles(self.template("AD8237"), "AD8237"),
            ["ground", "signal", "signal", "ground", "supply", "supply", "signal", "signal"],
        )

    def test_allows_verified_generated_wiring(self):
        cases = [
            ("AD8092", "B1_anthropic-claude-opus-5_library_k1.net"),
            ("ADA4177", "B2_anthropic-claude-opus-5_library_k2.net"),
            ("ADA4177", "B2_anthropic-claude-opus-5_library_k3.net"),
            ("AD8551", "B4_anthropic-claude-opus-5_k2.net"),
            ("ADR1399H", "A4_openai-gpt-5.6-sol_library_k1.net"),
        ]
        for part, filename in cases:
            with self.subTest(filename=filename):
                netlist = (NETLISTS / filename).read_text(encoding="utf-8")
                self.assertFalse(pin_role_mismatch(netlist, part, self.template(part)))

    def test_rejects_transposed_supply_rails(self):
        transposed = """\
VCC VCC 0 DC 5
VEE VEE 0 DC -5
XU1 IN FB VEE VCC OUT AD8092
.end
"""
        self.assertTrue(pin_role_mismatch(transposed, "AD8092", self.template("AD8092")))

    def test_audit_treats_role_mismatch_as_suspect_only_with_a_template(self):
        transposed = """\
VCC VCC 0 DC 5
VEE VEE 0 DC -5
XU1 IN FB VEE VCC OUT AD8092
.ac dec 20 1k 1Meg
.end
"""
        without_template = audit(transposed, "AD8092", "OUT", ["gain_db"])
        with_template = audit(
            transposed, "AD8092", "OUT", ["gain_db"], self.template("AD8092"))
        self.assertFalse(without_template.pin_role_mismatch)
        self.assertTrue(with_template.pin_role_mismatch)
        self.assertTrue(with_template.suspect)
        self.assertIn("pin_role_mismatch", with_template.flags)
        self.assertTrue(Audit(pin_role_mismatch=True).suspect)


if __name__ == "__main__":
    unittest.main()