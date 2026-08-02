from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


class E19InfrastructureContractTests(unittest.TestCase):
    def test_cosmos_provisions_identity_partition_and_web_api_write_access(self):
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")

        self.assertIn("resource appUsersContainer", cosmos)
        self.assertIn("name: 'app_users'", cosmos)
        self.assertIn("paths: ['/identity_key']", cosmos)
        self.assertNotIn("uniqueKeyPolicy", cosmos)
        self.assertIn(
            "scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/app_users'",
            cosmos,
        )
        self.assertIn(
            "scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/${containerName}'",
            cosmos,
        )
        self.assertNotIn("scope: cosmosAccount.id", cosmos)

    def test_legacy_cosmos_rbac_migration_is_fail_closed(self):
        migration = (
            ROOT / "scripts" / "migrate_e19_cosmos_rbac.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Assert-NarrowAssignments", migration)
        for contributor_container in [
            "sources", "watermarks", "runs", "dedup", "security_catalog",
            "market_data", "app_users", "portfolio_transactions",
            "ingestion_universe",
        ]:
            self.assertIn(f"{contributor_container} = $contributorRoleSuffix", migration)
        for reader_container in ["ingestion_universe", "portfolio_transactions", "security_catalog", "market_data"]:
            self.assertIn(f"{reader_container} = $readerRoleSuffix", migration)
        self.assertIn("$_.scope -eq $accountId", migration)
        self.assertIn("$_.principalId -in @($ingestPrincipalId, $webPrincipalId)", migration)
        self.assertIn("SupportsShouldProcess", migration)
        self.assertIn("--role-assignment-id $assignment.name", migration)

    def test_swa_uses_microsoft_personal_auth_and_linked_web_api(self):
        main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        function_app = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(encoding="utf-8")
        swa = (ROOT / "infra" / "modules" / "staticwebapp.bicep").read_text(encoding="utf-8")
        key_vault = (ROOT / "infra" / "modules" / "keyvault.bicep").read_text(encoding="utf-8")

        for setting in ["ENTRA_ISSUER", "ENTRA_AUDIENCE", "ENTRA_JWKS_URL"]:
            self.assertNotIn(setting, function_app)
        self.assertIn("APP_USERS_CONTAINER", function_app)
        self.assertIn("COSMOS_DATABASE_NAME", function_app)
        self.assertIn("param microsoftAuthClientId string", main)
        self.assertIn("param microsoftAuthClientSecret string", main)
        self.assertIn("Microsoft.Web/staticSites/config@2024-11-01", swa)
        self.assertIn("AZURE_CLIENT_ID: microsoftAuthClientId", swa)
        self.assertIn("param microsoftAuthClientSecret string", swa)
        self.assertIn(
            "AZURE_CLIENT_SECRET_APP_SETTING_NAME: microsoftAuthClientSecret",
            swa,
        )
        self.assertNotIn("@Microsoft.KeyVault", swa)
        self.assertIn("Microsoft.Web/staticSites/builds/linkedBackends@2024-11-01", swa)
        self.assertIn("backendResourceId: webApiResourceId", swa)
        self.assertIn("name: webApiName", swa)
        self.assertIn("output principalId string", swa)
        self.assertNotIn("resource microsoftAuthSecret", key_vault)
        self.assertNotIn("resource staticWebAppKvRole", key_vault)

    def test_swa_route_and_provider_policy_matches_mvp(self):
        config = json.loads(
            (ROOT / "web" / "public" / "staticwebapp.config.json").read_text(encoding="utf-8")
        )
        registration = config["auth"]["identityProviders"]["azureActiveDirectory"]["registration"]
        routes = config["routes"]

        self.assertEqual(registration["openIdIssuer"], "https://login.microsoftonline.com/consumers/v2.0")
        self.assertEqual(registration["clientIdSettingName"], "AZURE_CLIENT_ID")
        self.assertEqual(
            registration["clientSecretSettingName"],
            "AZURE_CLIENT_SECRET_APP_SETTING_NAME",
        )
        self.assertIn(
            {"route": "/api/*", "allowedRoles": ["authenticated"]},
            routes,
        )
        self.assertNotIn("/api/admin/*", [route["route"] for route in routes])
        self.assertIn({"route": "/.auth/login/google", "statusCode": 404}, routes)
        self.assertIn({"route": "/.auth/login/github", "statusCode": 404}, routes)

    def test_tracked_parameters_do_not_bind_a_tenant(self):
        for environment in ("dev", "prod"):
            parameters = json.loads(
                (ROOT / "infra" / "params" / f"{environment}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("fabricAdminUpn", parameters["parameters"])
            self.assertNotIn("onelakeWorkspaceId", parameters["parameters"])


if __name__ == "__main__":
    unittest.main()