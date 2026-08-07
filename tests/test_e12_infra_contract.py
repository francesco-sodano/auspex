from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class E12InfrastructureContractTests(unittest.TestCase):
    def test_dirty_company_events_are_company_partitioned_and_ingestion_owned(self):
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn("resource dirtyCompanyEventsContainer", cosmos)
        self.assertIn("name: 'dirty_company_events'", cosmos)
        self.assertIn("paths: ['/security_sk']", cosmos)
        self.assertIn("'dirty_company_events'", cosmos)

    def test_company_packages_have_ingestion_write_and_web_read_roles(self):
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn("resource companyPackagesContainer", cosmos)
        self.assertIn("name: 'company_packages'", cosmos)
        self.assertIn("paths: ['/security_sk']", cosmos)
        self.assertIn("resource webApiCompanyPackagesCosmosRole", cosmos)
        self.assertIn(
            "scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/company_packages'",
            cosmos,
        )

    def test_portfolio_transactions_are_owner_partitioned_and_narrowly_scoped(self):
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(
            encoding="utf-8"
        )
        function_app = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn("resource portfolioTransactionsContainer", cosmos)
        self.assertIn("name: 'portfolio_transactions'", cosmos)
        self.assertIn("paths: ['/owner_user_sk']", cosmos)
        self.assertIn("resource webApiPortfolioTransactionsCosmosRole", cosmos)
        self.assertIn(
            "scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/portfolio_transactions'",
            cosmos,
        )
        self.assertIn("name: 'PORTFOLIO_TRANSACTIONS_CONTAINER'", function_app)
        self.assertIn("value: 'portfolio_transactions'", function_app)

    def test_market_projection_containers_have_narrow_roles(self):
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(
            encoding="utf-8"
        )
        function_app = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(
            encoding="utf-8"
        )

        for container in [
            "security_catalog",
            "market_data",
            "ingestion_universe",
        ]:
            self.assertIn(f"name: '{container}'", cosmos)
            self.assertIn(
                f"scope: '${{cosmosAccount.id}}/dbs/${{databaseName}}/colls/{container}'",
                cosmos,
            )
        self.assertIn("cosmosDataReaderRoleId", cosmos)
        self.assertIn("resource ingestFuncPortfolioTransactionsCosmosRole", cosmos)
        self.assertIn("SECURITY_CATALOG_CONTAINER", function_app)
        self.assertIn("MARKET_DATA_CONTAINER", function_app)
        self.assertIn("INGESTION_UNIVERSE_CONTAINER", function_app)
        migration = (ROOT / "scripts" / "migrate_e19_cosmos_rbac.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$readerRoleSuffix", migration)
        for container in ["security_catalog", "market_data", "ingestion_universe", "portfolio_transactions"]:
            self.assertIn(container, migration)


if __name__ == "__main__":
    unittest.main()
