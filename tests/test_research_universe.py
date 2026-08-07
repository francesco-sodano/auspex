import unittest

from engine.research_universe import ResearchSecurity, resolve_research_universe


def security(security_sk, ticker, **overrides):
    values = {
        "security_sk": security_sk,
        "ticker": ticker,
        "is_active": True,
        "is_resolved": True,
        "is_price_covered": True,
        "theme_ids": ("ai_compute_semiconductors",),
    }
    values.update(overrides)
    return ResearchSecurity(**values)


class ResearchUniverseTests(unittest.TestCase):
    def test_non_held_theme_constituent_is_research_eligible(self):
        member = resolve_research_universe([security(1, "NVDA")])[0]

        self.assertTrue(member.included)
        self.assertEqual(member.tier, "eligible")

    def test_portfolio_holdings_are_an_override_not_the_universe_owner(self):
        members = resolve_research_universe([
            security(1, "NVDA"),
            security(
                2,
                "HELD",
                is_held=True,
                is_excluded=True,
                is_price_covered=False,
                theme_ids=(),
            ),
        ])

        self.assertEqual(members[0].tier, "held")
        self.assertIn("held_override", members[0].reasons)
        self.assertEqual(members[1].tier, "eligible")

    def test_unpriceable_non_held_security_remains_explicitly_excluded(self):
        member = resolve_research_universe([
            security(1, "NOPRICE", is_price_covered=False)
        ])[0]

        self.assertFalse(member.included)
        self.assertEqual(member.tier, "excluded")
        self.assertEqual(member.reasons, ("missing_price_coverage",))

    def test_watchlist_is_a_tier_not_a_portfolio_position(self):
        member = resolve_research_universe([
            security(1, "CAMT", is_watchlisted=True)
        ])[0]

        self.assertTrue(member.included)
        self.assertEqual(member.tier, "watchlist")


if __name__ == "__main__":
    unittest.main()