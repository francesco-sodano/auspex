import unittest

from engine.legacy_reset import (
    CONFIRMATION_TOKEN,
    OneLakeObject,
    WarehouseObject,
    build_reset_plan,
    preservation_manifest,
    require_confirmation,
    warehouse_drop_statements,
)


class LegacyEngineResetTests(unittest.TestCase):
    def test_plan_preserves_only_owner_identity_and_portfolio_ledger(self):
        plan = build_reset_plan(
            cosmos_containers=[
                "app_users",
                "portfolio_transactions",
                "decision_log",
                "market_data",
                "company_packages",
            ],
            onelake_paths=[
                OneLakeObject("lakehouse/Files/bronze", True),
                OneLakeObject("lakehouse/Tables/dbo", True),
            ],
            warehouse_objects=[WarehouseObject("dbo", "old_score", "U")],
            search_indexes=["idx-news-filings"],
            fabric_items=[
                {"id": "lakehouse", "displayName": "auspex_bronze", "type": "Lakehouse"},
                {"id": "warehouse", "displayName": "auspex_gold", "type": "Warehouse"},
                {"id": "notebook", "displayName": "nb_04_metrics", "type": "Notebook"},
            ],
        )

        self.assertEqual(plan.preserve_cosmos, ("app_users", "portfolio_transactions"))
        self.assertEqual(
            plan.purge_cosmos,
            ("company_packages", "decision_log", "market_data"),
        )
        self.assertEqual([item["id"] for item in plan.delete_fabric_items], ["notebook"])
        self.assertTrue(all(item.is_directory for item in plan.delete_onelake_paths))

    def test_preservation_manifest_is_stable_and_contains_documents(self):
        first = preservation_manifest({
            "app_users": [{"id": "user", "identity_key": "owner"}],
            "portfolio_transactions": [
                {"id": "trade-b", "owner_user_sk": "owner"},
                {"id": "trade-a", "owner_user_sk": "owner"},
            ],
        })
        replay = preservation_manifest({
            "portfolio_transactions": [
                {"id": "trade-a", "owner_user_sk": "owner"},
                {"id": "trade-b", "owner_user_sk": "owner"},
            ],
            "app_users": [{"id": "user", "identity_key": "owner"}],
        })

        self.assertEqual(first["sha256"], replay["sha256"])
        self.assertEqual(first["containers"]["portfolio_transactions"]["count"], 2)
        self.assertEqual(len(first["containers"]["portfolio_transactions"]["documents"]), 2)

    def test_apply_requires_exact_confirmation_token(self):
        with self.assertRaisesRegex(RuntimeError, CONFIRMATION_TOKEN):
            require_confirmation(True, "wrong")
        require_confirmation(True, CONFIRMATION_TOKEN)
        require_confirmation(False, "")

    def test_warehouse_objects_drop_dependents_before_tables(self):
        objects = (
            WarehouseObject("dbo", "score", "V"),
            WarehouseObject("dbo", "promote", "P"),
            WarehouseObject("dbo", "facts", "U"),
        )

        self.assertEqual(warehouse_drop_statements(objects), [
            "DROP VIEW [dbo].[score]",
            "DROP PROCEDURE [dbo].[promote]",
            "DROP TABLE [dbo].[facts]",
        ])


if __name__ == "__main__":
    unittest.main()