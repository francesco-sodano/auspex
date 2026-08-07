from datetime import datetime, timezone
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


VALID_COVERAGE = {"READY", "PARTIAL", "WITHHELD"}
VALID_OUTLOOKS = {"ACCELERATING", "STABLE", "DETERIORATING", "UNCERTAIN"}


class CompanyPackageNotFoundError(Exception):
    pass


class CompanyPackageRepository(Protocol):
    def list_current(self) -> list[dict]: ...
    def get_current(self, security_sk: int) -> dict | None: ...


class CosmosCompanyPackageRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def list_current(self) -> list[dict]:
        return list(self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.id = 'current' "
                "AND c.document_type = 'current'"
            ),
            enable_cross_partition_query=True,
        ))

    def get_current(self, security_sk: int) -> dict | None:
        try:
            document = self._container.read_item(
                item="current", partition_key=int(security_sk)
            )
            return document if document.get("document_type") == "current" else None
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return None


class InMemoryCompanyPackageRepository:
    def __init__(self, documents=None) -> None:
        self.documents = list(documents or [])

    def list_current(self) -> list[dict]:
        return [
            document for document in self.documents
            if document.get("id") == "current"
            and document.get("document_type") == "current"
        ]

    def get_current(self, security_sk: int) -> dict | None:
        return next((
            document for document in self.list_current()
            if int(document.get("security_sk") or 0) == int(security_sk)
        ), None)


class CompanyPackageService:
    def __init__(self, identity, repository: CompanyPackageRepository) -> None:
        self.identity = identity
        self.repository = repository

    def list_opportunities(
        self,
        principal_header,
        *,
        limit: int = 50,
        theme_id: str | None = None,
        coverage_status: str | None = None,
        outlook_direction: str | None = None,
    ) -> dict:
        self.identity.product_user(principal_header)
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if coverage_status is not None and coverage_status not in VALID_COVERAGE:
            raise ValueError("invalid coverage_status")
        if outlook_direction is not None and outlook_direction not in VALID_OUTLOOKS:
            raise ValueError("invalid outlook_direction")
        normalized_theme = str(theme_id or "").strip() or None
        documents = [
            document for document in self.repository.list_current()
            if normalized_theme is None or document.get("theme_id") == normalized_theme
        ]
        if coverage_status is not None:
            documents = [
                document for document in documents
                if document.get("coverage_status") == coverage_status
            ]
        if outlook_direction is not None:
            documents = [
                document for document in documents
                if document.get("outlook_direction") == outlook_direction
            ]
        documents.sort(key=lambda document: (
            -float(document.get("opportunity_score") or -1),
            str(document.get("ticker") or ""),
            int(document.get("security_sk") or 0),
        ))
        selected = documents[:limit]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(selected),
            "opportunities": [_payload(document) for document in selected],
            "disclaimer": "Company research only; not financial, tax, or investment advice.",
        }

    def get_opportunity(self, principal_header, security_sk: int) -> dict:
        self.identity.product_user(principal_header)
        if int(security_sk) <= 0:
            raise ValueError("security_sk must be positive")
        document = self.repository.get_current(int(security_sk))
        if document is None:
            raise CompanyPackageNotFoundError("company opportunity was not found")
        return _payload(document)


def _payload(document: dict) -> dict:
    allowed = {
        "package_version", "package_fingerprint", "security_sk", "ticker",
        "company_name", "as_of", "outlook_horizon_days", "outlook_direction",
        "theme_id", "classification_provenance", "candidate_count",
        "coverage_status", "coverage_reasons", "opportunity_score_raw",
        "opportunity_score", "model_version", "weight_version",
        "max_knowledge_date", "source_cursors", "legs", "evidence", "narrative",
    }
    return {
        **{key: value for key, value in document.items() if key in allowed},
        "research_only": True,
    }
