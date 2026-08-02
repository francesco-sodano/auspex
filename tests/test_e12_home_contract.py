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

    def test_ledger_reports_usd_currency_exposure_and_cash_stock_allocation(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("Portfolio value by currency", app)
        self.assertIn("Underlying currency; values converted to USD", app)
        self.assertIn("Portfolio allocation", app)
        self.assertIn("allocation-stocks", app)
        self.assertIn("allocation-cash", app)
        self.assertNotIn('fallback="Positions"', app)
        self.assertIn("Current assets", app)
        self.assertIn("Repeated transactions for one ticker are combined", app)
        self.assertIn("Missing current price", app)
        self.assertIn("<h2>Transactions</h2>", app)

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

    def test_ledger_exposes_repeatable_edits_without_audit_rows(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("corrects_transaction_id", app)
        self.assertIn("/correct`", app)
        self.assertIn("Edit transaction", app)
        self.assertIn("Save changes", app)
        self.assertNotIn("The original remains in the audit trail", app)
        self.assertNotIn("Superseded", app)
        self.assertNotIn("transaction.corrects_transaction_id || superseded", app)

    def test_home_exposes_bounded_deterministic_recommendations(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("fetch('/api/recommendations')", app)
        self.assertIn("Suggested actions", app)
        self.assertIn("Deterministic policy", app)
        self.assertIn("recommendation_service_unavailable", app)
        self.assertIn("recommendations.recommendations.slice(0, 12)", app)
        self.assertIn("recommendation-list", app)
        self.assertIn("recommendation-card-head", app)
        self.assertIn("aria-labelledby={`recommendation-", app)
        self.assertIn("<dt>Auspex score</dt>", app)
        self.assertNotIn('<table className="recommendation-table">', app)

    def test_home_has_analytical_holdings_coverage_and_current_analysis(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        for value in [
            "coverage-strip",
            "PriceSparkline",
            "seven latest sessions",
            "average_acquisition_price",
            "gain_loss_pct",
            "Auspex score",
            "Current portfolio analysis",
            "Strongest holding signal",
            "Score coverage",
        ]:
            self.assertIn(value, app)
        self.assertIn("['theme', 'exchange', 'country', 'currency']", app)
        self.assertNotIn("(['sector', 'country', 'currency']", app)

    def test_login_brand_and_loading_transition_are_purposeful(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        css = (ROOT / "web" / "src" / "App.css").read_text(encoding="utf-8")

        self.assertIn("function LoadingScreen", app)
        self.assertEqual(app.count("<LoadingScreen"), 2)
        self.assertIn("loading-ring", app)
        self.assertNotIn("home-loading", app)
        self.assertNotIn('<main className="loading">', app)
        self.assertIn("@keyframes loading-spin", css)
        self.assertIn(".login-panel .brand-lockup", css)
        self.assertIn(".loading-screen .brand-lockup{flex-direction:column", css)
        self.assertIn("flex-direction:column", css)


if __name__ == "__main__":
    unittest.main()