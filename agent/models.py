from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RecommendationDecision:
    decision_id: str
    owner_user_sk: str
    recommendation_id: str
    security_sk: int
    ticker: str
    action: str
    output_status: str
    input_snapshot_hash: str
    recommendation_model_version: str
    narrator_model_version: str
    prompt_version: str
    as_of: str
    output: dict
    evidence_pack: list[dict]
    reasons: tuple[str, ...]
    created_at: str

    def to_document(self) -> dict:
        document = asdict(self)
        document.update({
            "id": self.decision_id,
            "decision_type": "RECOMMENDATION",
            "schema_version": 1,
            "reasons": list(self.reasons),
        })
        return document

    @classmethod
    def from_document(cls, document: dict) -> "RecommendationDecision":
        return cls(
            **{
                field: tuple(document.get(field) or ()) if field == "reasons"
                else document.get(field)
                for field in cls.__dataclass_fields__
            }
        )

    def public_payload(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "recommendation_id": self.recommendation_id,
            "ticker": self.ticker,
            "action": self.action,
            "status": self.output_status,
            "as_of": self.as_of,
            "recommendation_model_version": self.recommendation_model_version,
            "narrator_model_version": self.narrator_model_version,
            "prompt_version": self.prompt_version,
            "output": self.output,
            "citations": self.evidence_pack,
            "reasons": list(self.reasons),
            "created_at": self.created_at,
            "disclaimer": "Research only; not financial or tax advice. You decide and execute.",
        }