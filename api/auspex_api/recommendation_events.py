from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Protocol, TYPE_CHECKING

from .owner_scoped import OwnerScope, OwnerScopedCosmosContainer

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DISPOSITIONS = {"ACCEPTED", "DISMISSED"}


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class RecommendationEvent:
    event_id: str
    owner_user_sk: str
    client_request_id: str
    recommendation_id: str
    ticker: str
    action: str
    disposition: str
    recommendation_as_of: str
    recommendation_snapshot: dict
    request_hash: str
    created_at: str

    def to_document(self) -> dict:
        return {
            **asdict(self),
            "id": f"event:{self.event_id}",
            "record_type": "RECOMMENDATION_DISPOSITION",
            "schema_version": 1,
        }

    @classmethod
    def from_document(cls, document: dict) -> "RecommendationEvent":
        return cls(**{field: document.get(field) for field in cls.__dataclass_fields__})

    def public_payload(self) -> dict:
        return {
            "event_id": self.event_id,
            "recommendation_id": self.recommendation_id,
            "ticker": self.ticker,
            "action": self.action,
            "disposition": self.disposition,
            "recommendation_as_of": self.recommendation_as_of,
            "recommendation": self.recommendation_snapshot,
            "created_at": self.created_at,
        }


class RecommendationEventRepository(Protocol):
    def read(self, owner_user_sk: str, event_id: str) -> RecommendationEvent | None: ...
    def append(
        self, owner_user_sk: str, event: RecommendationEvent,
    ) -> tuple[RecommendationEvent, bool]: ...
    def list(self, owner_user_sk: str) -> list[RecommendationEvent]: ...


class InMemoryRecommendationEventRepository:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], RecommendationEvent] = {}

    def read(self, owner_user_sk: str, event_id: str) -> RecommendationEvent | None:
        return self._events.get((owner_user_sk, event_id))

    def append(
        self, owner_user_sk: str, event: RecommendationEvent,
    ) -> tuple[RecommendationEvent, bool]:
        if event.owner_user_sk != owner_user_sk:
            raise ValueError("event owner does not match the authenticated owner")
        key = (owner_user_sk, event.event_id)
        existing = self._events.get(key)
        if existing is not None:
            if existing.request_hash != event.request_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing, False
        self._events[key] = event
        return event, True

    def list(self, owner_user_sk: str) -> list[RecommendationEvent]:
        return sorted(
            (
                event for (owner, _), event in self._events.items()
                if owner == owner_user_sk
            ),
            key=lambda event: (event.created_at, event.event_id),
        )


class CosmosRecommendationEventRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container
        self._documents = OwnerScopedCosmosContainer(container)

    def read(self, owner_user_sk: str, event_id: str) -> RecommendationEvent | None:
        document = self._documents.read(OwnerScope(owner_user_sk), f"event:{event_id}")
        return RecommendationEvent.from_document(document) if document else None

    def append(
        self, owner_user_sk: str, event: RecommendationEvent,
    ) -> tuple[RecommendationEvent, bool]:
        if event.owner_user_sk != owner_user_sk:
            raise ValueError("event owner does not match the authenticated owner")
        try:
            stored = self._documents.create(OwnerScope(owner_user_sk), event.to_document())
            return RecommendationEvent.from_document(stored), True
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            existing = self.read(owner_user_sk, event.event_id)
            if existing is None:
                raise
            if existing.request_hash != event.request_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing, False

    def list(self, owner_user_sk: str) -> list[RecommendationEvent]:
        documents = self._container.query_items(
            query=(
                "SELECT TOP 200 * FROM c WHERE c.record_type = "
                "'RECOMMENDATION_DISPOSITION'"
            ),
            partition_key=owner_user_sk,
        )
        return sorted(
            (RecommendationEvent.from_document(document) for document in documents),
            key=lambda event: (event.created_at, event.event_id),
        )


class RecommendationExperienceService:
    def __init__(
        self, identity, recommendations, decision_log, events, *, clock=None,
    ) -> None:
        self._identity = identity
        self._recommendations = recommendations
        self._decision_log = decision_log
        self._events = events
        self._clock = clock

    def record_disposition(
        self, principal_header, recommendation_id: str, payload: dict,
    ) -> tuple[dict, bool]:
        user = self._identity.product_user(principal_header)
        if not isinstance(payload, dict):
            raise ValueError("disposition payload must be an object")
        if "owner_user_sk" in payload:
            raise ValueError("owner_user_sk is server-controlled")
        client_request_id = payload.get("client_request_id")
        if not isinstance(client_request_id, str) or not _REQUEST_ID_PATTERN.fullmatch(
            client_request_id
        ):
            raise ValueError("client_request_id is invalid")
        disposition = str(payload.get("disposition") or "").upper()
        if disposition not in _DISPOSITIONS:
            raise ValueError("disposition must be ACCEPTED or DISMISSED")

        response = self._recommendations.recommendations(principal_header)
        if response.get("status") != "ready":
            raise ValueError("only a ready recommendation can receive a disposition")
        recommendation = next(
            (
                item for item in response.get("recommendations", [])
                if item.get("recommendation_id") == recommendation_id
            ),
            None,
        )
        if recommendation is None:
            raise ValueError("recommendation was not found in the current owner snapshot")

        request_hash = _canonical_hash({
            "recommendation_id": recommendation_id,
            "disposition": disposition,
        })
        event_id = _canonical_hash({
            "owner_user_sk": user.user_sk,
            "client_request_id": client_request_id,
        })
        existing = self._events.read(user.user_sk, event_id)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing.public_payload(), False

        now = self._clock() if self._clock else datetime.now(timezone.utc)
        event = RecommendationEvent(
            event_id=event_id,
            owner_user_sk=user.user_sk,
            client_request_id=client_request_id,
            recommendation_id=recommendation_id,
            ticker=str(recommendation["ticker"]),
            action=str(recommendation["action"]),
            disposition=disposition,
            recommendation_as_of=str(response.get("as_of") or recommendation.get("as_of")),
            recommendation_snapshot=dict(recommendation),
            request_hash=request_hash,
            created_at=now.isoformat(),
        )
        stored, created = self._events.append(user.user_sk, event)
        return stored.public_payload(), created

    def history(self, principal_header) -> dict:
        user = self._identity.product_user(principal_header)
        decisions = self._decision_log.list(user.user_sk)
        events = self._events.list(user.user_sk)
        current_dispositions: dict[str, str] = {}
        for event in events:
            current_dispositions[event.recommendation_id] = event.disposition
        return {
            "decisions": [
                decision.public_payload()
                for decision in sorted(
                    decisions,
                    key=lambda decision: (decision.created_at, decision.decision_id),
                    reverse=True,
                )
            ],
            "events": [event.public_payload() for event in reversed(events)],
            "current_dispositions": current_dispositions,
        }