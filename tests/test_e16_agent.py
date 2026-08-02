from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from agent.guardrails import GroundingViolation, validate_narration
from agent.service import GroundedRecommendationAgent
from api.auspex_api.decision_log import InMemoryDecisionLogRepository
from engine.completeness_gate import evaluate_recommendation_gate


ROOT = Path(__file__).resolve().parents[1]


class E16AgentTests(unittest.TestCase):
    @staticmethod
    def _recommendation_response(status="ready"):
        return {
            "status": status,
            "as_of": "2026-07-29",
            "recommendations": [{
                "recommendation_id": "rec-1",
                "security_sk": 101,
                "ticker": "MSFT",
                "action": "TRIM",
                "current_weight": "0.20",
                "target_weight": "0.10",
                "suggested_amount_base": "-1000.00",
                "estimated_cost_base": "7.50",
                "expected_edge_base": "92.50",
                "confidence": "HIGH",
                "rationale": "Trim MSFT to the deterministic target.",
                "suppression_reasons": [],
                "tax_flags": [],
                "as_of": "2026-07-29",
                "model_version": "e15_v1",
            }],
        }

    @staticmethod
    def _citations():
        return [{
            "id": "evidence-1",
            "security_sk": 101,
            "symbol": "MSFT",
            "title": "Filed evidence",
            "excerpt": "Microsoft filed its update on 2026-07-29.",
            "event_date": "2026-07-29",
            "knowledge_date": "2026-07-29",
            "content_status": "summary",
        }]

    def test_portfolio_factory_constructs_service_before_recommendations(self):
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")

        portfolio_factory = function_app.split("def _portfolio_service()", 1)[1].split(
            "def _recommendation_service()", 1
        )[0]
        self.assertIn("return PortfolioService(", portfolio_factory)

    def test_gate_requires_fresh_ready_portfolio_and_pit_evidence(self):
        recommendation = {
            "status": "ready",
            "as_of": "2026-07-29",
            "recommendations": [{"recommendation_id": "rec-1"}],
        }
        citations = [{"id": "evidence-1", "knowledge_date": "2026-07-29"}]

        result = evaluate_recommendation_gate(
            recommendation,
            citations,
            today=date(2026, 7, 30),
        )

        self.assertTrue(result.ready)
        self.assertEqual(result.reasons, ())

        future_evidence = [{"id": "evidence-1", "knowledge_date": "2026-07-30"}]
        withheld = evaluate_recommendation_gate(
            recommendation,
            future_evidence,
            today=date(2026, 7, 30),
        )
        self.assertFalse(withheld.ready)
        self.assertIn("evidence_after_recommendation_as_of", withheld.reasons)

    def test_guardrail_rejects_invented_numbers_and_citations(self):
        recommendation = {
            "recommendation_id": "rec-1",
            "ticker": "MSFT",
            "action": "TRIM",
            "current_weight": "0.20",
            "target_weight": "0.10",
            "suggested_amount_base": "-1000.00",
            "estimated_cost_base": "7.50",
            "rationale": "Trim MSFT to the deterministic target.",
        }
        citations = [{
            "id": "evidence-1",
            "symbol": "MSFT",
            "title": "Filed evidence",
            "excerpt": "Microsoft filed its update on 2026-07-29.",
            "knowledge_date": "2026-07-29",
        }]

        with self.assertRaises(GroundingViolation):
            validate_narration(
                {
                    "recommendation_id": "rec-1",
                    "ticker": "MSFT",
                    "action": "TRIM",
                    "explanation": "Trim MSFT because expected upside is 37 percent.",
                    "uncertainty": "Evidence is limited.",
                    "evidence_ids": ["evidence-1"],
                },
                recommendation,
                self._citations(),
            )

        with self.assertRaises(GroundingViolation):
            validate_narration(
                {
                    "recommendation_id": "rec-1",
                    "ticker": "MSFT",
                    "action": "TRIM",
                    "explanation": "Trim MSFT while NVDA remains a stronger alternative.",
                    "uncertainty": "Evidence is limited.",
                    "evidence_ids": ["evidence-1"],
                },
                recommendation,
                citations,
            )

    def test_guardrail_allows_deterministic_weight_percentage(self):
        recommendation = self._recommendation_response()["recommendations"][0]
        output = validate_narration(
            {
                "recommendation_id": "rec-1",
                "ticker": "MSFT",
                "action": "TRIM",
                "explanation": "Trim MSFT toward the supplied 10 percent target weight.",
                "uncertainty": "The cited filing is limited evidence.",
                "evidence_ids": ["evidence-1"],
            },
            recommendation,
            self._citations(),
        )

        self.assertEqual(output["action"], "TRIM")

        with self.assertRaises(GroundingViolation):
            validate_narration(
                {
                    "recommendation_id": "rec-1",
                    "ticker": "MSFT",
                    "action": "TRIM",
                    "explanation": "Trim MSFT to the deterministic target.",
                    "uncertainty": "Evidence is limited.",
                    "evidence_ids": ["invented-source"],
                },
                recommendation,
                self._citations(),
            )

    def test_agent_contract_is_owner_implicit_and_decision_log_is_append_only(self):
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
        function_infra = (
            ROOT / "infra" / "modules" / "functionapp.bicep"
        ).read_text(encoding="utf-8")
        openai = (ROOT / "infra" / "modules" / "openai.bicep").read_text(encoding="utf-8")
        deployment = (
            ROOT / "scripts" / "deploy_e7_functions.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('route="recommendations/{recommendation_id}/explain"', function_app)
        self.assertNotIn("owner_user_sk", function_app.split(
            'route="recommendations/{recommendation_id}/explain"', 1
        )[0].split("@app.route", 1)[-1])
        self.assertIn("decision_log", cosmos)
        self.assertIn("paths: ['/owner_user_sk']", cosmos)
        self.assertIn("webApiDecisionLogCosmosRole", cosmos)
        self.assertIn("DECISION_LOG_CONTAINER", function_infra)
        self.assertIn("AZURE_OPENAI_CHAT_MODEL_VERSION", function_infra)
        self.assertIn("webApiFuncOpenAiRole", openai)
        self.assertIn('Copy-Item (Join-Path $repositoryRoot "agent")', deployment)

    def test_agent_instructions_forbid_action_math_and_uncited_claims(self):
        instructions = (
            ROOT / "agent" / "foundry_config" / "instructions.txt"
        ).read_text(encoding="utf-8")

        for phrase in [
            "Never change the supplied action",
            "Never perform arithmetic",
            "Never introduce a ticker",
            "Cite only supplied evidence IDs",
            "Never claim that a return is guaranteed",
        ]:
            self.assertIn(phrase, instructions)

    def test_grounded_agent_publishes_once_and_replay_skips_model(self):
        class Narrator:
            model_version = "gpt-4o:2024-11-20"

            def __init__(self):
                self.calls = 0

            def narrate(self, recommendation, citations):
                self.calls += 1
                return {
                    "recommendation_id": recommendation["recommendation_id"],
                    "ticker": recommendation["ticker"],
                    "action": recommendation["action"],
                    "explanation": "Trim MSFT to the deterministic target.",
                    "uncertainty": "The cited filing is limited evidence.",
                    "evidence_ids": [citations[0]["id"]],
                }

        narrator = Narrator()
        decisions = InMemoryDecisionLogRepository()
        agent = GroundedRecommendationAgent(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: self._recommendation_response()),
            SimpleNamespace(retrieve=lambda **_: self._citations()),
            narrator,
            decisions,
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        first = agent.explain("principal-a", "rec-1")
        replay = agent.explain("principal-a", "rec-1")

        self.assertEqual(first, replay)
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["citations"][0]["id"], "evidence-1")
        self.assertEqual(narrator.calls, 1)

    def test_decision_identity_ignores_search_ranking_score_jitter(self):
        class Narrator:
            model_version = "gpt-4o:2024-11-20"

            def __init__(self):
                self.calls = 0

            def narrate(self, recommendation, citations):
                self.calls += 1
                return {
                    "recommendation_id": "rec-1",
                    "ticker": "MSFT",
                    "action": "TRIM",
                    "explanation": "Trim MSFT to the deterministic target.",
                    "uncertainty": "The cited filing is limited evidence.",
                    "evidence_ids": ["evidence-1"],
                }

        retrieval_count = 0

        def retrieve(**_):
            nonlocal retrieval_count
            retrieval_count += 1
            return [{**self._citations()[0], "score": float(retrieval_count)}]

        narrator = Narrator()
        agent = GroundedRecommendationAgent(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: self._recommendation_response()),
            SimpleNamespace(retrieve=retrieve),
            narrator,
            InMemoryDecisionLogRepository(),
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        first = agent.explain("principal-a", "rec-1")
        replay = agent.explain("principal-a", "rec-1")

        self.assertEqual(first["decision_id"], replay["decision_id"])
        self.assertEqual(narrator.calls, 1)

    def test_decision_log_strips_non_https_evidence_links(self):
        narrator = SimpleNamespace(
            model_version="gpt-4o:2024-11-20",
            narrate=lambda recommendation, citations: {
                "recommendation_id": "rec-1",
                "ticker": "MSFT",
                "action": "TRIM",
                "explanation": "Trim MSFT to the deterministic target.",
                "uncertainty": "The cited filing is limited evidence.",
                "evidence_ids": ["evidence-1"],
            },
        )
        malicious_citation = {**self._citations()[0], "url": "javascript:alert(1)"}
        agent = GroundedRecommendationAgent(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: self._recommendation_response()),
            SimpleNamespace(retrieve=lambda **_: [malicious_citation]),
            narrator,
            InMemoryDecisionLogRepository(),
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        result = agent.explain("principal-a", "rec-1")

        self.assertEqual(result["status"], "published")
        self.assertIsNone(result["citations"][0]["url"])

    def test_grounded_agent_withholds_stale_input_without_model_call(self):
        class Narrator:
            model_version = "gpt-4o:2024-11-20"
            calls = 0

            def narrate(self, recommendation, citations):
                self.calls += 1
                raise AssertionError("stale input must not reach the narrator")

        narrator = Narrator()
        agent = GroundedRecommendationAgent(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: self._recommendation_response("stale")),
            SimpleNamespace(retrieve=lambda **_: self._citations()),
            narrator,
            InMemoryDecisionLogRepository(),
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        result = agent.explain("principal-a", "rec-1")

        self.assertEqual(result["status"], "withheld")
        self.assertIn("portfolio_or_signal_status_not_ready", result["reasons"])
        self.assertEqual(narrator.calls, 0)

    def test_grounded_agent_withholds_hallucinated_model_output(self):
        narrator = SimpleNamespace(
            model_version="gpt-4o:2024-11-20",
            narrate=lambda recommendation, citations: {
                "recommendation_id": "rec-1",
                "ticker": "MSFT",
                "action": "TRIM",
                "explanation": "Trim MSFT because expected upside is 37 percent.",
                "uncertainty": "The cited filing is limited evidence.",
                "evidence_ids": ["evidence-1"],
            },
        )
        agent = GroundedRecommendationAgent(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: self._recommendation_response()),
            SimpleNamespace(retrieve=lambda **_: self._citations()),
            narrator,
            InMemoryDecisionLogRepository(),
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        result = agent.explain("principal-a", "rec-1")

        self.assertEqual(result["status"], "withheld")
        self.assertEqual(result["output"], {})
        self.assertEqual(result["citations"], [])
        self.assertIn("narration_grounding_failed", result["reasons"])

    def test_grounded_agent_withholds_evidence_source_failure(self):
        class Narrator:
            model_version = "gpt-4o:2024-11-20"
            calls = 0

            def narrate(self, recommendation, citations):
                self.calls += 1
                raise AssertionError("failed evidence must not reach the narrator")

        narrator = Narrator()

        def fail_evidence(**_):
            raise RuntimeError("search unavailable")

        agent = GroundedRecommendationAgent(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: self._recommendation_response()),
            SimpleNamespace(retrieve=fail_evidence),
            narrator,
            InMemoryDecisionLogRepository(),
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

        result = agent.explain("principal-a", "rec-1")

        self.assertEqual(result["status"], "withheld")
        self.assertIn("evidence_source_failure", result["reasons"])
        self.assertEqual(narrator.calls, 0)

    def test_home_requests_and_renders_grounded_explanations(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("/explain`", app)
        self.assertIn("Explain with evidence", app)
        self.assertIn("Explanation withheld:", app)
        self.assertIn("citation.knowledge_date", app)


if __name__ == "__main__":
    unittest.main()