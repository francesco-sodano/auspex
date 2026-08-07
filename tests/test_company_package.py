from dataclasses import replace
from datetime import date
import unittest

from engine.company_package import (
    PACKAGE_VERSION,
    CompanyLegState,
    CompanyOpportunityPackage,
    CompanySourceCursor,
    EvidenceRef,
    build_company_package,
    classify_outlook,
    package_changed,
    package_document,
    package_fingerprint,
    validate_company_package,
)
from engine.thesis import LEG_WEIGHTS, MODEL_VERSION, WEIGHT_VERSION, OpportunityResult


AS_OF = date(2026, 8, 7)


def evidence(evidence_id="document-1", knowledge_date=AS_OF):
    return EvidenceRef(
        evidence_id=evidence_id,
        source_type="sec_filing",
        source_id="filing:1",
        revision_hash="a" * 64,
        event_date=date(2026, 8, 6),
        knowledge_date=knowledge_date,
        retention_class="public_filing",
        url="https://www.sec.gov/example",
        excerpt="Management raised its current capacity plan.",
    )


def package(raw_score=0.4, evidence_rows=None):
    evidence_rows = tuple(evidence_rows or (evidence(),))
    legs = tuple(
        CompanyLegState(
            leg_name=leg_name,
            normalized_value=0.5,
            contribution=0.1,
            direction="RAISED",
            available_component_weight=1.0,
            coverage_reasons=(),
            evidence_ids=(evidence_rows[0].evidence_id,),
            max_knowledge_date=evidence_rows[0].knowledge_date,
        )
        for leg_name in LEG_WEIGHTS
    )
    return CompanyOpportunityPackage(
        package_version=PACKAGE_VERSION,
        security_sk=42,
        ticker="NVDA",
        company_name="NVIDIA Corporation",
        as_of=AS_OF,
        outlook_horizon_days=90,
        outlook_direction=classify_outlook(raw_score, "READY"),
        theme_id="ai_compute_semiconductors",
        classification_provenance="manual",
        classification_id="classification-1",
        candidate_count=19,
        coverage_status="READY",
        coverage_reasons=(),
        opportunity_score_raw=raw_score,
        opportunity_score=81.2,
        model_version=MODEL_VERSION,
        weight_version=WEIGHT_VERSION,
        max_knowledge_date=AS_OF,
        source_cursors=(CompanySourceCursor(
            source_class="sec_filings",
            source_id="sec_8k",
            latest_record_id="0001-26-000001",
            latest_revision_hash="a" * 64,
            latest_knowledge_date=AS_OF,
        ),),
        legs=legs,
        evidence=evidence_rows,
    )


class CompanyOpportunityPackageTests(unittest.TestCase):
    def test_package_document_is_json_safe_and_content_addressed(self):
        document = package_document(package())

        self.assertEqual(document["document_type"], "revision")
        self.assertEqual(document["id"], f"package:{document['package_fingerprint']}")
        self.assertEqual(document["as_of"], "2026-08-07")

    def test_six_leg_result_builds_one_lineage_backed_company_package(self):
        current = package()
        result = OpportunityResult(
            score_id="score-1",
            cohort_snapshot_hash="cohort-1",
            theme_id=current.theme_id,
            security_sk=current.security_sk,
            date_sk=20260807,
            as_of=current.as_of,
            classification_provenance=current.classification_provenance,
            classification_id=current.classification_id,
            classification_updated_at=date(2026, 8, 7),
            candidate_count=current.candidate_count,
            thesis_linkage_z=0.5,
            attention_acceleration_z=0.5,
            smart_money_z=0.5,
            fundamental_health_z=0.5,
            valuation_brake_z=0.5,
            crowding_positioning_z=0.5,
            thesis_linkage_contribution=0.1,
            attention_acceleration_contribution=0.1,
            smart_money_contribution=0.1,
            fundamental_health_contribution=0.1,
            valuation_brake_contribution=0.1,
            crowding_positioning_contribution=0.1,
            opportunity_score_raw=0.4,
            opportunity_score=81.2,
            coverage_status="READY",
            coverage_reasons=(),
            max_knowledge_date=AS_OF,
            model_version=MODEL_VERSION,
            weight_version=WEIGHT_VERSION,
        )
        source = evidence()

        built = build_company_package(
            result,
            ticker="nvda",
            company_name="NVIDIA Corporation",
            leg_available_component_weights={leg_name: 1.0 for leg_name in LEG_WEIGHTS},
            leg_evidence={leg_name: (source,) for leg_name in LEG_WEIGHTS},
        )

        self.assertEqual(built.ticker, "NVDA")
        self.assertEqual(len(built.legs), 6)
        self.assertEqual(len(built.evidence), 1)
        self.assertTrue(all(leg.evidence_ids == (source.evidence_id,) for leg in built.legs))

    def test_future_source_cursor_is_rejected(self):
        current = package()
        future_cursor = replace(
            current.source_cursors[0],
            latest_knowledge_date=date(2026, 8, 8),
        )

        with self.assertRaisesRegex(ValueError, "source cursor contains future knowledge"):
            validate_company_package(replace(current, source_cursors=(future_cursor,)))

    def test_directional_result_without_leg_evidence_fails_closed(self):
        current = package()
        result = OpportunityResult(
            score_id="score-1",
            cohort_snapshot_hash="cohort-1",
            theme_id=current.theme_id,
            security_sk=current.security_sk,
            date_sk=20260807,
            as_of=current.as_of,
            classification_provenance=current.classification_provenance,
            classification_id=current.classification_id,
            classification_updated_at=date(2026, 8, 7),
            candidate_count=current.candidate_count,
            thesis_linkage_z=0.5,
            attention_acceleration_z=0.5,
            smart_money_z=0.5,
            fundamental_health_z=0.5,
            valuation_brake_z=0.5,
            crowding_positioning_z=0.5,
            thesis_linkage_contribution=0.1,
            attention_acceleration_contribution=0.1,
            smart_money_contribution=0.1,
            fundamental_health_contribution=0.1,
            valuation_brake_contribution=0.1,
            crowding_positioning_contribution=0.1,
            opportunity_score_raw=0.4,
            opportunity_score=81.2,
            coverage_status="READY",
            coverage_reasons=(),
            max_knowledge_date=AS_OF,
            model_version=MODEL_VERSION,
            weight_version=WEIGHT_VERSION,
        )

        with self.assertRaisesRegex(ValueError, "requires evidence lineage"):
            build_company_package(
                result,
                ticker="NVDA",
                company_name="NVIDIA Corporation",
                leg_available_component_weights={leg_name: 1.0 for leg_name in LEG_WEIGHTS},
                leg_evidence={},
            )

    def test_valid_package_has_stable_order_independent_fingerprint(self):
        first = package(evidence_rows=(evidence("document-1"), evidence("document-2")))
        second = replace(
            first,
            legs=tuple(reversed(first.legs)),
            evidence=tuple(reversed(first.evidence)),
        )

        validate_company_package(first)
        self.assertEqual(package_fingerprint(first), package_fingerprint(second))

    def test_package_changes_only_when_content_changes(self):
        first = package()
        replay = replace(first, legs=tuple(reversed(first.legs)))
        changed = replace(
            first,
            opportunity_score_raw=0.6,
            outlook_direction="ACCELERATING",
        )

        self.assertFalse(package_changed(first, replay))
        self.assertTrue(package_changed(first, changed))
        self.assertTrue(package_changed(None, first))

    def test_directional_leg_requires_resolvable_evidence(self):
        current = package()
        broken_leg = replace(current.legs[0], evidence_ids=("missing-document",))

        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            validate_company_package(replace(current, legs=(broken_leg, *current.legs[1:])))

    def test_future_knowledge_is_rejected(self):
        future = evidence(knowledge_date=date(2026, 8, 8))

        with self.assertRaisesRegex(ValueError, "future knowledge"):
            validate_company_package(package(evidence_rows=(future,)))

    def test_withheld_package_is_uncertain(self):
        current = package(raw_score=None)
        unavailable = tuple(
            replace(
                leg,
                normalized_value=None,
                contribution=None,
                direction="UNAVAILABLE",
                available_component_weight=0.0,
                coverage_reasons=("missing:source",),
                evidence_ids=(),
                max_knowledge_date=None,
            )
            for leg in current.legs
        )
        withheld = replace(
            current,
            outlook_direction="UNCERTAIN",
            coverage_status="WITHHELD",
            coverage_reasons=("no_available_legs",),
            opportunity_score=None,
            legs=unavailable,
            evidence=(),
        )

        validate_company_package(withheld)
        self.assertEqual(package_fingerprint(withheld), package_fingerprint(withheld))


if __name__ == "__main__":
    unittest.main()