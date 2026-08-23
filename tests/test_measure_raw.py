"""Regression tests for raw waveform measurements."""

import math
import unittest

import numpy as np

from measure_raw import measure_tran_gain_db


class FakeTraces:
    variables = ["time", "V(Vin+)", "V(Vin-)", "V(Vout)"]

    def __init__(self):
        time = np.linspace(0.0, 0.004, 4_001)
        vin_positive = np.sin(2.0 * np.pi * 1_000.0 * time)
        vin_negative = -vin_positive
        self.data = {
            "V(Vin+)": vin_positive,
            "V(Vin-)": vin_negative,
            "V(Vout)": 10.0 * (vin_positive - vin_negative),
        }

    def get_data(self, name, case=0):
        return self.data.get(name)


class MeasureRawTests(unittest.TestCase):
    def test_transient_gain_accepts_differential_input_expression(self):
        gain_db = measure_tran_gain_db(
            FakeTraces(),
            in_node="Vin+-Vin-",
            out_node="Vout",
        )

        self.assertTrue(math.isclose(gain_db, 20.0, abs_tol=0.01))


if __name__ == "__main__":
    unittest.main()