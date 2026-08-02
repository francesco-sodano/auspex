from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import TYPE_CHECKING

from .owner_scoped import OwnerScope, OwnerScopedCosmosContainer

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


def _default_advisor(risk_profile: str | None) -> str:
    posture = {
        "Conservative": "Emphasize capital preservation, concentration, and evidence limitations.",
        "Balanced": "Balance growth opportunities with concentration, cash, costs, and uncertainty.",
        "Growth": "Emphasize long-horizon growth while clearly stating concentration and valuation risk.",
        "Aggressive": "Discuss high-conviction growth while making downside, concentration, and uncertainty explicit.",
    }
    return posture.get(
        risk_profile,
        "Use concise plain language and make uncertainty and evidence limitations explicit.",
    )


@dataclass(frozen=True)
class DiscussionExchange:
    exchange_id: str
    owner_user_sk: str
    conversation_id: str
    client_request_id: str
    request_hash: str
    query: str
    status: str
    answer: str
    confidence: str
    limitations: str
    evidence_pack: list[dict]
    metric_keys: tuple[str, ...]
    what_if: dict | None
    input_snapshot_hash: str
    model_version: str
    prompt_version: str
    reasons: tuple[str, ...]
    created_at: str

    def to_document(self) -> dict:
        return {
            **asdict(self),
            "id": f"discussion:{self.exchange_id}",
            "record_type": "DISCUSSION_EXCHANGE",
            "metric_keys": list(self.metric_keys),
            "reasons": list(self.reasons),
            "schema_version": 1,
        }

    @classmethod
    def from_document(cls, document: dict) -> "DiscussionExchange":
        values = {field: document.get(field) for field in cls.__dataclass_fields__}
        values["metric_keys"] = tuple(values.get("metric_keys") or ())
        values["reasons"] = tuple(values.get("reasons") or ())
        return cls(**values)

    def public_payload(self) -> dict:
        return {
            "exchange_id": self.exchange_id,
            "conversation_id": self.conversation_id,
            "query": self.query,
            "status": self.status,
            "answer": self.answer,
            "confidence": self.confidence,
            "limitations": self.limitations,
            "citations": self.evidence_pack,
            "metric_keys": list(self.metric_keys),
            "what_if": self.what_if,
            "reasons": list(self.reasons),
            "created_at": self.created_at,
            "disclaimer": "Research only; not financial, tax, or legal advice. Auspex never executes trades.",
        }

    def context_payload(self) -> dict:
        return {
            "query": self.query,
            "answer": self.answer,
            "status": self.status,
            "created_at": self.created_at,
        }


class InMemoryDiscussionRepository:
    exchange_type = DiscussionExchange

    def __init__(self) -> None:
        self._exchanges: dict[tuple[str, str], DiscussionExchange] = {}
        self._profiles: dict[str, dict] = {}
        self._preferences: dict[str, dict] = {}

    def read_exchange(self, owner_user_sk: str, exchange_id: str):
        return self._exchanges.get((owner_user_sk, exchange_id))

    def append_exchange(self, owner_user_sk: str, exchange: DiscussionExchange):
        if exchange.owner_user_sk != owner_user_sk:
            raise ValueError("exchange owner does not match authenticated owner")
        key = (owner_user_sk, exchange.exchange_id)
        existing = self._exchanges.get(key)
        if existing is not None:
            if existing.request_hash != exchange.request_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing, False
        self._exchanges[key] = exchange
        return exchange, True

    def list_exchanges(self, owner_user_sk: str, conversation_id: str, limit: int):
        rows = sorted(
            (
                row for (owner, _), row in self._exchanges.items()
                if owner == owner_user_sk and row.conversation_id == conversation_id
            ),
            key=lambda row: (row.created_at, row.exchange_id),
        )
        return rows[-limit:]

    def get_advisor_profile(self, owner_user_sk: str, risk_profile: str | None):
        return self._profiles.get(owner_user_sk) or {
            "instructions": _default_advisor(risk_profile),
            "is_default": True,
            "prompt_version": "e18_advisor_v1",
            "risk_profile": risk_profile,
        }

    def save_advisor_profile(self, owner_user_sk: str, risk_profile, instructions: str):
        profile = {
            "instructions": instructions,
            "is_default": False,
            "prompt_version": "e18_advisor_v1",
            "risk_profile": risk_profile,
        }
        self._profiles[owner_user_sk] = profile
        return profile

    def reset_advisor_profile(self, owner_user_sk: str, risk_profile):
        self._profiles.pop(owner_user_sk, None)
        return self.get_advisor_profile(owner_user_sk, risk_profile)

    def get_preferences(self, owner_user_sk: str):
        return self._preferences.get(owner_user_sk)

    def save_preferences(self, owner_user_sk: str, preferences: dict):
        self._preferences[owner_user_sk] = dict(preferences)
        return dict(preferences)


class CosmosDiscussionRepository:
    exchange_type = DiscussionExchange

    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container
        self._documents = OwnerScopedCosmosContainer(container)

    def read_exchange(self, owner_user_sk: str, exchange_id: str):
        document = self._documents.read(
            OwnerScope(owner_user_sk), f"discussion:{exchange_id}"
        )
        return DiscussionExchange.from_document(document) if document else None

    def append_exchange(self, owner_user_sk: str, exchange: DiscussionExchange):
        if exchange.owner_user_sk != owner_user_sk:
            raise ValueError("exchange owner does not match authenticated owner")
        try:
            stored = self._documents.create(OwnerScope(owner_user_sk), exchange.to_document())
            return DiscussionExchange.from_document(stored), True
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            existing = self.read_exchange(owner_user_sk, exchange.exchange_id)
            if existing is None:
                raise
            if existing.request_hash != exchange.request_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing, False

    def list_exchanges(self, owner_user_sk: str, conversation_id: str, limit: int):
        documents = self._container.query_items(
            query=(
                "SELECT TOP 200 * FROM c WHERE c.record_type = 'DISCUSSION_EXCHANGE' "
                "AND c.conversation_id = @conversation_id"
            ),
            parameters=[{"name": "@conversation_id", "value": conversation_id}],
            partition_key=owner_user_sk,
        )
        rows = sorted(
            (DiscussionExchange.from_document(document) for document in documents),
            key=lambda row: (row.created_at, row.exchange_id),
        )
        return rows[-limit:]

    def _read_document(self, owner_user_sk: str, document_id: str):
        return self._documents.read(OwnerScope(owner_user_sk), document_id)

    def _save_document(self, owner_user_sk: str, document_id: str, document: dict):
        scope = OwnerScope(owner_user_sk)
        existing = self._documents.read(scope, document_id)
        if existing is not None:
            stored = self._documents.replace(scope, document_id, document)
            if stored is None:
                raise RuntimeError("owner document disappeared during replace")
            return stored
        try:
            return self._documents.create(scope, {**document, "id": document_id})
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            stored = self._documents.replace(scope, document_id, document)
            if stored is None:
                raise
            return stored

    def get_advisor_profile(self, owner_user_sk: str, risk_profile: str | None):
        document = self._read_document(owner_user_sk, "advisor:profile")
        if document is None:
            return {
                "instructions": _default_advisor(risk_profile),
                "is_default": True,
                "prompt_version": "e18_advisor_v1",
                "risk_profile": risk_profile,
            }
        return {
            "instructions": document["instructions"],
            "is_default": False,
            "prompt_version": document["prompt_version"],
            "risk_profile": document.get("risk_profile"),
        }

    def save_advisor_profile(self, owner_user_sk: str, risk_profile, instructions: str):
        document = self._save_document(owner_user_sk, "advisor:profile", {
            "record_type": "ADVISOR_PROFILE",
            "instructions": instructions,
            "prompt_version": "e18_advisor_v1",
            "risk_profile": risk_profile,
            "schema_version": 1,
        })
        return {
            "instructions": document["instructions"],
            "is_default": False,
            "prompt_version": document["prompt_version"],
            "risk_profile": document.get("risk_profile"),
        }

    def reset_advisor_profile(self, owner_user_sk: str, risk_profile):
        try:
            self._container.delete_item(
                item="advisor:profile", partition_key=owner_user_sk
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
        return self.get_advisor_profile(owner_user_sk, risk_profile)

    def get_preferences(self, owner_user_sk: str):
        document = self._read_document(owner_user_sk, "notification:preferences")
        return dict(document) if document else None

    def save_preferences(self, owner_user_sk: str, preferences: dict):
        return self._save_document(owner_user_sk, "notification:preferences", {
            **preferences,
            "record_type": "NOTIFICATION_PREFERENCES",
            "schema_version": 1,
        })


class NotificationPreferenceService:
    def __init__(
        self, identity, portfolio, recommendations, repository, *, clock=None,
    ) -> None:
        self._identity = identity
        self._portfolio = portfolio
        self._recommendations = recommendations
        self._repository = repository
        self._clock = clock

    def preferences(self, principal_header) -> dict:
        user = self._identity.product_user(principal_header)
        stored = self._repository.get_preferences(user.user_sk) or {}
        return {
            "in_app_enabled": bool(stored.get("in_app_enabled", True)),
            "email_opt_in": False,
            "email_available": False,
            "email_unavailable_reason": (
                "Email delivery is unavailable because Auspex restricts data resources to "
                "Switzerland North and Azure Communication Services Email is global."
            ),
            "contact_email": user.contact_email,
        }

    def update_preferences(self, principal_header, payload: dict) -> dict:
        user = self._identity.product_user(principal_header)
        if not isinstance(payload, dict) or "owner_user_sk" in payload:
            raise ValueError("notification preferences payload is invalid")
        if payload.get("email_opt_in") is True:
            raise ValueError("email delivery is not available under the region policy")
        in_app_enabled = payload.get("in_app_enabled", True)
        if not isinstance(in_app_enabled, bool):
            raise ValueError("in_app_enabled must be boolean")
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        self._repository.save_preferences(user.user_sk, {
            "in_app_enabled": in_app_enabled,
            "email_opt_in": False,
            "updated_at": now.isoformat(),
        })
        return self.preferences(principal_header)

    def morning_summary(self, principal_header) -> dict:
        user = self._identity.product_user(principal_header)
        portfolio = self._portfolio.portfolio_summary(principal_header)
        recommendations = self._recommendations.recommendations(principal_header)
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        top_suggestion = next(
            (
                row for row in recommendations.get("recommendations", [])
                if row.get("action") != "HOLD"
            ),
            None,
        )
        return {
            "status": "ready" if portfolio.get("status") == "ready" else "withheld",
            "summary_date": now.date().isoformat(),
            "valuation_as_of": portfolio.get("valuation_as_of"),
            "base_currency": portfolio.get("base_currency") or user.base_currency,
            "portfolio_value_base": portfolio.get("total_value_base"),
            "cash_base": portfolio.get("total_cash_base"),
            "holding_count": len(portfolio.get("holdings", [])),
            "top_suggestion": top_suggestion,
            "delivery_channel": "IN_APP",
            "app_path": "/discussion",
            "limitations": (
                "What changed is unavailable until two comparable daily portfolio snapshots exist."
            ),
        }