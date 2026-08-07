from dataclasses import replace
from datetime import date
import unittest

from engine.company_updates import (
    DirtyCompanyChange,
    decide_package_publication,
    plan_company_updates,
)
from tests.test_company_package import package


def change(change_id, security_sk, source_class, knowledge_date=date(2026, 8, 7)):
    return DirtyCompanyChange(
        change_id=change_id,
        security_sk=security_sk,
        source_class=source_class,
        source_id=f"source:{source_class}",
        knowledge_date=knowledge_date,
    )


class CompanyUpdatePlannerTests(unittest.TestCase):
    def test_changes_coalesce_by_company_and_dirty_only_affected_themes(self):
        plan = plan_company_updates(
            [
                change("dirty:price", 42, "prices", date(2026, 8, 6)),
                change("dirty:filing", 42, "fundamentals"),
                change("dirty:other", 84, "news"),
            ],
            theme_ids_by_security={
                42: ("ai_compute_semiconductors",),
                84: ("healthcare",),
                99: ("energy_security_producers",),
            },
        )

        self.assertEqual([update.security_sk for update in plan.companies], [42, 84])
        self.assertEqual(
            plan.companies[0].source_classes,
            ("fundamentals", "prices"),
        )
        self.assertEqual(
            plan.dirty_theme_ids,
            ("ai_compute_semiconductors", "healthcare"),
        )
        self.assertNotIn("energy_security_producers", plan.dirty_theme_ids)

    def test_no_changes_is_successful_noop(self):
        plan = plan_company_updates([], theme_ids_by_security={})

        self.assertTrue(plan.is_noop)
        self.assertEqual(plan.dirty_theme_ids, ())

    def test_unclassified_company_remains_visible_for_package_processing(self):
        plan = plan_company_updates(
            [change("dirty:news", 42, "news")],
            theme_ids_by_security={},
        )

        self.assertEqual(len(plan.companies), 1)
        self.assertEqual(plan.unclassified_security_sks, (42,))
        self.assertEqual(plan.dirty_theme_ids, ())

    def test_unchanged_package_completes_events_without_new_revision(self):
        current = package()
        decision = decide_package_publication(
            current,
            current,
            change_ids=("dirty:b", "dirty:a"),
        )

        self.assertFalse(decision.publish_revision)
        self.assertEqual(decision.complete_change_ids, ("dirty:a", "dirty:b"))

    def test_changed_package_publishes_one_revision(self):
        current = package()
        changed = replace(
            current,
            opportunity_score_raw=0.6,
            outlook_direction="ACCELERATING",
        )
        decision = decide_package_publication(
            current,
            changed,
            change_ids=("dirty:a",),
        )

        self.assertTrue(decision.publish_revision)
        self.assertEqual(len(decision.package_fingerprint), 64)


if __name__ == "__main__":
    unittest.main()