from datetime import date, timedelta
import unittest

from scripts.backtest_opportunity_score import evaluate


class OpportunityBacktestTests(unittest.TestCase):
    def test_reports_insufficient_history_without_forward_window(self):
        scores = [{
            "as_of": "2026-08-06", "theme_id": "theme", "ticker": "A",
            "benchmark_symbol": "BM", "opportunity_score_raw": 1.0,
            "beta_252d": 1.0,
        }]
        prices = [
            {"ticker": "A", "event_date": "2026-08-06", "close": 10},
            {"ticker": "BM", "event_date": "2026-08-06", "close": 10},
        ]

        result = evaluate(scores, prices, bootstrap_iterations=10)

        self.assertEqual(result["63"]["status"], "insufficient_history")
        self.assertEqual(result["126"]["cohort_dates"], 0)

    def test_computes_top_quintile_beta_adjusted_spread(self):
        start = date(2025, 1, 2)
        tickers = ["A", "B", "C", "D", "E"]
        scores = [
            {
                "as_of": start.isoformat(),
                "theme_id": "theme",
                "ticker": ticker,
                "benchmark_symbol": "BM",
                "opportunity_score_raw": float(5 - index),
                "beta_252d": 1.0,
            }
            for index, ticker in enumerate(tickers)
        ]
        prices = []
        for offset in range(127):
            event_date = (start + timedelta(days=offset)).isoformat()
            prices.append({"ticker": "BM", "event_date": event_date, "close": 100 + offset})
            for index, ticker in enumerate(tickers):
                multiplier = 2.0 if ticker == "A" else 1.0 + index * 0.01
                prices.append({
                    "ticker": ticker,
                    "event_date": event_date,
                    "close": 100 + offset * multiplier,
                })

        result = evaluate(scores, prices, bootstrap_iterations=10)

        self.assertEqual(result["63"]["status"], "evaluated")
        self.assertEqual(result["63"]["cohort_dates"], 1)
        self.assertEqual(result["63"]["top_quintile_rows"], 1)
        self.assertGreater(result["63"]["mean_beta_adjusted_spread"], 0)
        self.assertEqual(result["126"]["status"], "evaluated")


if __name__ == "__main__":
    unittest.main()
