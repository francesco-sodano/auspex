from dataclasses import dataclass
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


@dataclass(frozen=True)
class OwnerScope:
    owner_user_sk: str

    def __post_init__(self) -> None:
        if not self.owner_user_sk:
            raise ValueError("owner_user_sk is required")


class OwnerScopedCosmosContainer:
    """Cosmos operations that always use owner_user_sk as the partition key."""

    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def create(self, scope: OwnerScope, document: dict) -> dict:
        body = dict(document)
        supplied_owner = body.get("owner_user_sk")
        if supplied_owner not in (None, scope.owner_user_sk):
            raise ValueError("document owner does not match the authenticated owner")
        body["owner_user_sk"] = scope.owner_user_sk
        body.setdefault("id", str(uuid.uuid4()))
        return self._container.create_item(body)

    def read(self, scope: OwnerScope, document_id: str) -> dict | None:
        try:
            return self._container.read_item(
                item=document_id,
                partition_key=scope.owner_user_sk,
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return None

    def replace(self, scope: OwnerScope, document_id: str, document: dict) -> dict | None:
        replacement = dict(document)
        supplied_owner = replacement.get("owner_user_sk")
        if supplied_owner not in (None, scope.owner_user_sk):
            raise ValueError("document owner does not match the authenticated owner")
        replacement["id"] = document_id
        replacement["owner_user_sk"] = scope.owner_user_sk
        try:
            return self._container.replace_item(item=document_id, body=replacement)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return None

    def delete(self, scope: OwnerScope, document_id: str) -> bool:
        try:
            self._container.delete_item(
                item=document_id,
                partition_key=scope.owner_user_sk,
            )
            return True
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return False