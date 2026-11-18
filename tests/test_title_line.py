"""SPICE discards line 1 as the title. ensure_title_line must add a synthetic
title when line 1 is a real statement, and leave a prose title alone."""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)
from app.simulation_runner import ensure_title_line  # noqa: E402

TITLE = "* (auto-inserted title; SPICE discards line 1 unconditionally)"


class EnsureTitleLine(unittest.TestCase):
    def test_comment_first_line_untouched(self):
        net = "* my title\nV1 IN 0 5\n.end\n"
        self.assertEqual(ensure_title_line(net), net)

    def test_element_first_line_gets_title(self):
        # The original bug: line 1 was the supply, swallowed as the title.
        net = "V1 IN 0 5\nR1 IN OUT 1k\n.end\n"
        self.assertTrue(ensure_title_line(net).startswith(TITLE))

    def test_directive_first_line_gets_title(self):
        net = ".lib LT1720.sub\nX1 A B VCC 0 OUT LT1720\n.end\n"
        self.assertTrue(ensure_title_line(net).startswith(TITLE))

    def test_prose_title_left_alone(self):
        # Regression: an extra title pushed this to line 2, where it parsed as
        # an inductor named T1720 and broke a netlist that had simulated.
        net = ("LT1720 Comparator with Open-Drain Output\n"
               "V1 VCC 0 5\nX1 A B VCC 0 OUT LT1720\n.end\n")
        self.assertEqual(ensure_title_line(net), net)

    def test_prose_title_that_parses_as_an_element(self):
        # Syntactically a valid inductor; its "nodes" connect to nothing.
        net = "LT3045-1 5V to 3.3V LDO Stage\nV1 IN 0 5\nX1 IN OUT LT3045-1\n.end\n"
        self.assertEqual(ensure_title_line(net), net)

    def test_ground_in_title_is_not_evidence(self):
        net = "LT3094 0 to -5V regulator\nV1 IN 0 -7\nX1 IN OUT LT3094\n.end\n"
        self.assertEqual(ensure_title_line(net), net)


if __name__ == "__main__":
    unittest.main()
