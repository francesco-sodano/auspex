from typing import Protocol, TYPE_CHECKING

from agent.models import RecommendationDecision

from .owner_scoped import OwnerScope, OwnerScopedCosmosContainer

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


class DecisionLogRepository(Protocol):
    def read(self, owner_user_sk: str, decision_id: str) -> RecommendationDecision | None: ...
    def append(
        self,
        owner_user_sk: str,
        decision: RecommendationDecision,
    ) -> tuple[RecommendationDecision, bool]: ...
    def list(self, owner_user_sk: str) -> list[RecommendationDecision]: ...


class InMemoryDecisionLogRepository:
    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], RecommendationDecision] = {}

    def read(self, owner_user_sk: str, decision_id: str) -> RecommendationDecision | None:
        return self._documents.get((owner_user_sk, decision_id))

    def append(
        self,
        owner_user_sk: str,
        decision: RecommendationDecision,
    ) -> tuple[RecommendationDecision, bool]:
        if decision.owner_user_sk != owner_user_sk:
            raise ValueError("decision owner does not match the authenticated owner")
        key = (owner_user_sk, decision.decision_id)
        existing = self._documents.get(key)
        if existing is not None:
            return existing, False
        self._documents[key] = decision
        return decision, True

    def list(self, owner_user_sk: str) -> list[RecommendationDecision]:
        return [
            decision for (owner, _), decision in self._documents.items()
            if owner == owner_user_sk
        ]


class CosmosDecisionLogRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container
        self._documents = OwnerScopedCosmosContainer(container)

    def read(self, owner_user_sk: str, decision_id: str) -> RecommendationDecision | None:
        scope = OwnerScope(owner_user_sk)
        document = self._documents.read(scope, decision_id)
        return RecommendationDecision.from_document(document) if document else None

    def append(
        self,
        owner_user_sk: str,
        decision: RecommendationDecision,
    ) -> tuple[RecommendationDecision, bool]:
        scope = OwnerScope(owner_user_sk)
        if decision.owner_user_sk != owner_user_sk:
            raise ValueError("decision owner does not match the authenticated owner")
        try:
            stored = self._documents.create(scope, decision.to_document())
            return RecommendationDecision.from_document(stored), True
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            existing = self.read(owner_user_sk, decision.decision_id)
            if existing is None:
                raise
            return existing, False

    def list(self, owner_user_sk: str) -> list[RecommendationDecision]:
        documents = self._container.query_items(
            query="SELECT TOP 200 * FROM c WHERE c.decision_type = 'RECOMMENDATION'",
            partition_key=owner_user_sk,
        )
        return [RecommendationDecision.from_document(document) for document in documents]