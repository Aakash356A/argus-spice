"""Fast numerical checks for waveform-level linearity metrics."""

import unittest

import numpy as np

from bench.waveform import CLIP_LIMIT, THD_LIMIT, _waveform_metrics, linearity_violation


class WaveformMetricTests(unittest.TestCase):
    def setUp(self):
        self.frequency = 1_000.0
        self.time = np.linspace(0.0, 0.020, 20_001)

    def test_clean_sine_has_low_thd_and_no_clipping_violation(self):
        output = np.sin(2 * np.pi * self.frequency * self.time)
        metrics = _waveform_metrics(self.time, output, self.frequency)
        self.assertIsNotNone(metrics)
        self.assertLess(metrics["thd"], 0.01)
        self.assertLess(metrics["clipped"], CLIP_LIMIT)

    def test_second_harmonic_exceeds_thd_limit(self):
        output = (np.sin(2 * np.pi * self.frequency * self.time)
                  + 0.2 * np.sin(4 * np.pi * self.frequency * self.time))
        metrics = _waveform_metrics(self.time, output, self.frequency)
        self.assertIsNotNone(metrics)
        self.assertGreater(metrics["thd"], THD_LIMIT)

    def test_non_transient_netlist_skips_without_simulating(self):
        netlist = """\
V1 IN 0 SINE(0 1 1k)
R1 OUT IN 1k
.ac dec 10 1 10k
.end
"""
        violation, details = linearity_violation(netlist, "IN", "OUT", self.frequency)
        self.assertFalse(violation)
        self.assertEqual(details, {"skipped": "no transient analysis"})


if __name__ == "__main__":
    unittest.main()