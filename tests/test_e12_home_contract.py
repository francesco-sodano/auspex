from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class E12HomeContractTests(unittest.TestCase):
    def test_home_uses_owner_scoped_portfolio_summary_without_mock_values(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("fetch('/api/portfolio_summary')", app)
        for label in [
            "Portfolio value · cash + stocks",
            "Net contributed capital",
            "Total gain / loss",
            "Cash available",
            "Holdings",
            "Coverage & freshness",
            "Portfolio exposure",
        ]:
            self.assertIn(label, app)
        self.assertIn("pending_ingestion", app)
        self.assertIn("Add your first ledger entry", app)
        self.assertNotIn("142,350", app)
        self.assertNotIn("22,350", app)

    def test_transaction_lookup_and_retries_are_race_safe(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("setResolvedSecurity(null); setSecurityLookupError('')", app)
        self.assertIn("const query = securityCode.trim().toUpperCase()", app)
        self.assertIn("setResolvedSecurity(exact ? { ...exact, query } : null)", app)
        self.assertIn("client_request_id: draftRequestId", app)
        self.assertIn("Transaction saved, but refresh failed", app)
        self.assertIn("!ledgerLoaded", app)
        self.assertIn("price_currency", app)
        self.assertIn("holding.price_currency || holding.currency", app)

    def test_ledger_copy_and_number_controls_are_concise_and_bounded(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertNotIn("Source of truth", app)
        self.assertNotIn("Start with opening cash, one stock, both, or nothing.", app)
        self.assertNotIn("Choose <strong>Opening cash</strong>", app)
        self.assertNotIn('className="starting-note"', app)
        self.assertIn("Updated {formatUpdatedOn(summary.updated_on)}", app)
        self.assertIn('min="0.01" max="999999999999.99" step="0.01"', app)
        self.assertIn('min="0.00000001" max="1000000000" step="0.00000001"', app)

    def test_transaction_modal_exposes_search_state_limits_and_fx(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("/api/stock/search?q=", app)
        self.assertIn('role="listbox"', app)
        self.assertIn("Available to sell:", app)
        self.assertIn("selectedHolding?.quantity", app)
        self.assertIn("Cannot exceed available cash", app)
        self.assertIn("Opening cash (starting balance)", app)
        self.assertIn("Opening position (already owned)", app)
        self.assertIn("FX rate to {user.base_currency}", app)

    def test_ledger_exposes_append_only_correction_workflow(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("corrects_transaction_id", app)
        self.assertIn("/correct`", app)
        self.assertIn("Correct transaction", app)
        self.assertIn("The original remains in the audit trail", app)
        self.assertIn("Superseded", app)

    def test_home_exposes_bounded_deterministic_recommendations(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("fetch('/api/recommendations')", app)
        self.assertIn("Suggested actions", app)
        self.assertIn("Deterministic policy", app)
        self.assertIn("recommendation_service_unavailable", app)
        self.assertIn("recommendations.recommendations.slice(0, 12)", app)

    def test_login_brand_and_loading_transition_are_purposeful(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        css = (ROOT / "web" / "src" / "App.css").read_text(encoding="utf-8")

        self.assertIn("loading-ring", app)
        self.assertIn("@keyframes loading-spin", css)
        self.assertIn(".login-panel .brand-lockup", css)
        self.assertIn("flex-direction:column", css)


if __name__ == "__main__":
    unittest.main()