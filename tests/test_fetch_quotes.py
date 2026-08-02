import unittest

import fetch_quotes as fq


class QuoteSummaryTests(unittest.TestCase):
    def test_summarize_daily_returns_only_lightweight_facts(self) -> None:
        rows = []
        for index in range(6):
            close = 10.0 + index
            rows.append({
                "trade_date": f"2026-07-{24 + index:02d}",
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1000.0 + index * 100,
                "amount": 100_000_000.0 + index * 10_000_000,
                "turnover_rate": 2.5,
                "pre_close": None if index == 0 else close - 1.0,
                "pct_chg": None if index == 0 else (close / (close - 1.0) - 1) * 100,
            })

        snapshot = fq.summarize_daily(rows, "2026-07-29")

        self.assertEqual("2026-07-29", snapshot["trade_date"])
        self.assertTrue(snapshot["is_target_trade_date"])
        self.assertAlmostEqual(50.0, snapshot["return_5d_pct"])
        self.assertAlmostEqual(1.25, snapshot["volume_ratio_5d"])
        self.assertEqual(1.5, snapshot["amount_yi"])
        self.assertNotIn("ma20", snapshot)
        self.assertNotIn("signal", snapshot)

    def test_non_trading_target_marks_snapshot_as_stale(self) -> None:
        rows = [{
            "trade_date": "2026-07-31", "open": 10, "high": 11, "low": 9,
            "close": 10.5, "volume": 1000, "amount": None,
            "turnover_rate": None, "pre_close": 10, "pct_chg": 5,
        }]
        snapshot = fq.summarize_daily(rows, "2026-08-01")
        self.assertFalse(snapshot["is_target_trade_date"])


if __name__ == "__main__":
    unittest.main()
