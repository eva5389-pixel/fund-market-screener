import importlib.util
import math
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).parents[1] / "src" / "update_dashboard.py"
SPEC = importlib.util.spec_from_file_location("update_dashboard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MetricsTest(unittest.TestCase):
    def test_percent_change(self):
        self.assertAlmostEqual(MODULE.pct_change([100, 110], 1), 0.10)

    def test_max_drawdown(self):
        self.assertAlmostEqual(MODULE.max_drawdown([100, 120, 90, 108]), -0.25)

    def test_recovery_days(self):
        self.assertEqual(MODULE.recovery_days([100, 120, 90, 110, 120]), 2)

    def test_sharpe_handles_short_series(self):
        self.assertIsNone(MODULE.sharpe([100, 101], 0.015))

    def test_score_does_not_treat_zero_as_missing(self):
        row = {"return_1y": 0, "excess_return_1y": 0, "momentum_6m": 0,
               "sharpe": 0, "max_drawdown": 0}
        self.assertEqual(MODULE.score(row), 0)

    def test_score_is_blank_when_metrics_are_incomplete(self):
        self.assertIsNone(MODULE.score({"return_1y": 0.1}))

    def test_yahoo_symbol_candidates(self):
        self.assertEqual(MODULE.yahoo_symbol_candidates("F0GBR04AMX:FO"),
                         ["F0GBR04AMX:FO", "F0GBR04AMX"])
        self.assertEqual(MODULE.yahoo_symbol_candidates("F0GBR04AMX"),
                         ["F0GBR04AMX"])


if __name__ == "__main__":
    unittest.main()
