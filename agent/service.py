from datetime import date, datetime, timezone
import hashlib
import json
import logging

from engine.completeness_gate import evaluate_recommendation_gate

from .guardrails import GroundingViolation, validate_narration
from .models import RecommendationDecision
from .narrator import PROMPT_VERSION


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _evidence_snapshot(citations: list[dict]) -> list[dict]:
    fields = (
        "id", "security_sk", "symbol", "source_type", "source_id", "source_name",
        "url", "title", "excerpt", "event_date", "knowledge_date", "revision_hash",
        "content_status",
    )
    snapshots = []
    for citation in citations:
        snapshot = {field: citation.get(field) for field in fields}
        url = str(snapshot.get("url") or "")
        snapshot["url"] = url if url.startswith("https://") else None
        snapshots.append(snapshot)
    return sorted(
        snapshots,
        key=lambda citation: str(citation.get("id") or ""),
    )


class GroundedRecommendationAgent:
    def __init__(
        self,
        identity,
        recommendations,
        evidence,
        narrator,
        decision_log,
        *,
        clock=None,
    ) -> None:
        self._identity = identity
        self._recommendations = recommendations
        self._evidence = evidence
        self._narrator = narrator
        self._decision_log = decision_log
        self._clock = clock

    def explain(self, principal_header, recommendation_id: str) -> dict:
        user = self._identity.product_user(principal_header)
        owner_user_sk = user.user_sk
        response = self._recommendations.recommendations(principal_header)
        recommendation = next(
            (
                item for item in response.get("recommendations", [])
                if item.get("recommendation_id") == recommendation_id
            ),
            None,
        )
        if recommendation is None:
            raise ValueError("recommendation was not found in the current owner snapshot")

        as_of_text = response.get("as_of")
        try:
            as_of = date.fromisoformat(as_of_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("recommendation as_of is invalid") from exc
        evidence_failed = False
        try:
            citations = self._evidence.retrieve(
                query=(
                    f"{recommendation['ticker']} {recommendation['action']} "
                    f"{recommendation.get('rationale') or ''}"
                ),
                as_of=as_of,
                security_sks=[int(recommendation["security_sk"])],
                limit=8,
            )
        except Exception:
            logging.exception("E16 evidence retrieval failed")
            citations = []
            evidence_failed = True
        citations = _evidence_snapshot(citations)
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        gate = evaluate_recommendation_gate(
            response,
            citations,
            today=now.date(),
        )
        input_payload = {
            "owner_user_sk": owner_user_sk,
            "recommendation": recommendation,
            "citations": citations,
            "recommendation_as_of": as_of_text,
            "narrator_model_version": self._narrator.model_version,
            "prompt_version": PROMPT_VERSION,
        }
        input_snapshot_hash = _canonical_hash(input_payload)
        decision_id = _canonical_hash({
            "owner_user_sk": owner_user_sk,
            "input_snapshot_hash": input_snapshot_hash,
            "decision_type": "RECOMMENDATION",
        })
        existing = self._decision_log.read(owner_user_sk, decision_id)
        if existing is not None:
            return existing.public_payload()

        reasons = list(gate.reasons)
        if evidence_failed:
            reasons.append("evidence_source_failure")
        output = {}
        status = "withheld"
        if gate.ready:
            try:
                output = validate_narration(
                    self._narrator.narrate(recommendation, citations),
                    recommendation,
                    citations,
                )
                status = "published"
            except GroundingViolation:
                reasons.append("narration_grounding_failed")
            except Exception:
                logging.exception("E16 narrator failed")
                reasons.append("narration_unavailable")

        cited_ids = set(output.get("evidence_ids") or [])
        cited_evidence = [
            citation for citation in citations
            if citation.get("id") in cited_ids
        ] if status == "published" else []
        decision = RecommendationDecision(
            decision_id=decision_id,
            owner_user_sk=owner_user_sk,
            recommendation_id=recommendation_id,
            security_sk=int(recommendation["security_sk"]),
            ticker=recommendation["ticker"],
            action=recommendation["action"],
            output_status=status,
            input_snapshot_hash=input_snapshot_hash,
            recommendation_model_version=recommendation.get("model_version") or "unknown",
            narrator_model_version=self._narrator.model_version,
            prompt_version=PROMPT_VERSION,
            as_of=as_of_text,
            output=output,
            evidence_pack=cited_evidence,
            reasons=tuple(dict.fromkeys(reasons)),
            created_at=now.isoformat(),
        )
        stored, _ = self._decision_log.append(owner_user_sk, decision)
        return stored.public_payload()