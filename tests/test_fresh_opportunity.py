from datetime import date
import unittest

from engine.company_package import CompanySourceCursor, EvidenceRef, LEG_WEIGHTS
from engine.fresh_opportunity import FreshCompanySignal, score_fresh_theme


AS_OF = date(2026, 8, 7)


def signal(security_sk, ticker, value, missing=()):
    evidence = EvidenceRef(
        evidence_id=f"evidence:{ticker}",
        source_type="fresh_packet",
        source_id=f"company:{ticker}",
        revision_hash=(str(security_sk) * 64)[:64],
        event_date=AS_OF,
        knowledge_date=AS_OF,
        retention_class="company_package",
        excerpt=f"Fresh source packet for {ticker}",
    )
    raw_values = {
        leg_name: None if leg_name in missing else value
        for leg_name in LEG_WEIGHTS
    }
    return FreshCompanySignal(
        security_sk=security_sk,
        ticker=ticker,
        company_name=f"{ticker} Inc.",
        as_of=AS_OF,
        theme_id="theme",
        classification_provenance="curated_v1",
        classification_id=f"classification:{ticker}",
        raw_leg_values=raw_values,
        leg_evidence={
            leg_name: () if leg_name in missing else (evidence,)
            for leg_name in LEG_WEIGHTS
        },
        leg_coverage_reasons={
            leg_name: ("missing:fresh_source",) if leg_name in missing else ()
            for leg_name in LEG_WEIGHTS
        },
        source_cursors=(CompanySourceCursor(
            source_class="fresh_packet",
            source_id=f"company:{ticker}",
            latest_record_id=f"packet:{ticker}",
            latest_revision_hash=evidence.revision_hash,
            latest_knowledge_date=AS_OF,
        ),),
    )


class FreshOpportunityEngineTests(unittest.TestCase):
    def test_higher_fresh_signal_ranks_above_peers(self):
        packages = score_fresh_theme([
            signal(1, "LOW", -1.0),
            signal(2, "MID", 0.0),
            signal(3, "HIGH", 1.0),
        ])

        by_ticker = {package.ticker: package for package in packages}
        self.assertEqual(by_ticker["HIGH"].outlook_direction, "ACCELERATING")
        self.assertEqual(by_ticker["LOW"].outlook_direction, "DETERIORATING")
        self.assertGreater(
            by_ticker["HIGH"].opportunity_score,
            by_ticker["MID"].opportunity_score,
        )
        self.assertTrue(all(package.coverage_status == "READY" for package in packages))

    def test_partial_company_remains_scored_with_explicit_missing_leg(self):
        packages = score_fresh_theme([
            signal(1, "LOW", -1.0),
            signal(2, "MID", 0.0, missing=("crowding_positioning",)),
            signal(3, "HIGH", 1.0),
        ])
        middle = next(package for package in packages if package.ticker == "MID")

        self.assertEqual(middle.coverage_status, "PARTIAL")
        crowding = next(
            leg for leg in middle.legs if leg.leg_name == "crowding_positioning"
        )
        self.assertEqual(crowding.direction, "UNAVAILABLE")
        self.assertIn("missing:fresh_source", crowding.coverage_reasons)

    def test_small_theme_is_withheld(self):
        packages = score_fresh_theme([
            signal(1, "ONE", 1.0),
            signal(2, "TWO", 2.0),
        ])

        self.assertTrue(all(package.coverage_status == "WITHHELD" for package in packages))
        self.assertTrue(all(package.outlook_direction == "UNCERTAIN" for package in packages))

    def test_available_leg_without_evidence_fails_closed(self):
        broken = signal(1, "BROKEN", 1.0)
        broken = FreshCompanySignal(
            **{**broken.__dict__, "leg_evidence": {**broken.leg_evidence, "smart_money": ()}}
        )

        with self.assertRaisesRegex(ValueError, "smart_money requires evidence lineage"):
            score_fresh_theme([broken, signal(2, "TWO", 0.0), signal(3, "THREE", -1.0)])


if __name__ == "__main__":
    unittest.main()