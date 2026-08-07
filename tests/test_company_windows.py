import unittest

from engine.company_windows import ACTIVE_WINDOW_POLICIES, validate_active_window_policies


class CompanyWindowPolicyTests(unittest.TestCase):
    def test_compact_windows_cover_all_six_legs(self):
        validate_active_window_policies()
        covered_legs = {
            leg
            for policy in ACTIVE_WINDOW_POLICIES.values()
            for leg in policy.legs
        }

        self.assertEqual(covered_legs, {
            "thesis_linkage",
            "attention_acceleration",
            "smart_money",
            "fundamental_health",
            "valuation_brake",
            "crowding_positioning",
        })

    def test_price_and_news_windows_are_bounded(self):
        self.assertEqual(ACTIVE_WINDOW_POLICIES["prices"].lookback_days, 30)
        self.assertEqual(ACTIVE_WINDOW_POLICIES["news"].lookback_days, 60)
        self.assertEqual(
            ACTIVE_WINDOW_POLICIES["institutional_holdings"].snapshot_count,
            2,
        )
        self.assertEqual(ACTIVE_WINDOW_POLICIES["fundamentals"].snapshot_count, 8)


if __name__ == "__main__":
    unittest.main()