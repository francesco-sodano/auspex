from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy

from .models import AppUser, AuthenticatedPrincipal, RegistrationAcknowledgments


class AppUserRepository(Protocol):
    def get_by_principal(self, principal: AuthenticatedPrincipal) -> AppUser | None: ...
    def create_pending(self, principal: AuthenticatedPrincipal, acknowledgments: RegistrationAcknowledgments, versions: dict[str, str]) -> tuple[AppUser, bool]: ...
    def create_admin(self, principal: AuthenticatedPrincipal) -> tuple[AppUser, bool]: ...
    def list_by_status(self, status: str) -> list[AppUser]: ...
    def get_by_user_sk(self, user_sk: str) -> AppUser | None: ...
    def replace(self, user: AppUser) -> AppUser: ...


class CosmosAppUserRepository:
    def __init__(self, container: "ContainerProxy", clock=None) -> None:
        self._container = container
        self._clock = clock

    def get_by_principal(self, principal: AuthenticatedPrincipal) -> AppUser | None:
        try:
            document = self._container.read_item(
                item=principal.identity_key,
                partition_key=principal.identity_key,
            )
            return AppUser.from_document(document)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return None

    def _create(self, user: AppUser) -> tuple[AppUser, bool]:
        try:
            document = self._container.create_item(user.to_document())
            return AppUser.from_document(document), True
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            document = self._container.read_item(
                item=user.identity_key,
                partition_key=user.identity_key,
            )
            return AppUser.from_document(document), False

    def create_pending(self, principal, acknowledgments, versions):
        existing = self.get_by_principal(principal)
        if existing is not None:
            return existing, False
        return self._create(AppUser.pending_registration(
            principal,
            acknowledgments,
            versions,
            now=self._clock() if self._clock else None,
        ))

    def create_admin(self, principal):
        existing = self.get_by_principal(principal)
        if existing is not None:
            return existing, False
        return self._create(AppUser.bootstrap_admin(
            principal,
            now=self._clock() if self._clock else None,
        ))

    def list_by_status(self, status):
        documents = self._container.query_items(
            query="SELECT * FROM c WHERE c.status = @status ORDER BY c.created_at",
            parameters=[{"name": "@status", "value": status}],
            enable_cross_partition_query=True,
        )
        return [AppUser.from_document(document) for document in documents]

    def get_by_user_sk(self, user_sk):
        documents = list(self._container.query_items(
            query="SELECT * FROM c WHERE c.user_sk = @user_sk",
            parameters=[{"name": "@user_sk", "value": user_sk}],
            enable_cross_partition_query=True,
            max_item_count=2,
        ))
        if len(documents) > 1:
            raise RuntimeError(f"duplicate app_user user_sk: {user_sk}")
        return AppUser.from_document(documents[0]) if documents else None

    def replace(self, user):
        document = self._container.replace_item(
            item=user.identity_key,
            body=user.to_document(),
        )
        return AppUser.from_document(document)


class InMemoryAppUserRepository:
    def __init__(self, clock=None) -> None:
        self._documents: dict[str, dict] = {}
        self._clock = clock

    def get_by_principal(self, principal):
        document = self._documents.get(principal.identity_key)
        return AppUser.from_document(document) if document else None

    def _create(self, user):
        existing = self._documents.get(user.identity_key)
        if existing:
            return AppUser.from_document(existing), False
        self._documents[user.identity_key] = user.to_document()
        return user, True

    def create_pending(self, principal, acknowledgments, versions):
        return self._create(AppUser.pending_registration(
            principal,
            acknowledgments,
            versions,
            now=self._clock() if self._clock else None,
        ))

    def create_admin(self, principal):
        return self._create(AppUser.bootstrap_admin(
            principal,
            now=self._clock() if self._clock else None,
        ))

    def list_by_status(self, status):
        users = [
            AppUser.from_document(document)
            for document in self._documents.values()
            if document["status"] == status
        ]
        return sorted(users, key=lambda user: user.created_at)

    def get_by_user_sk(self, user_sk):
        matches = [
            AppUser.from_document(document)
            for document in self._documents.values()
            if document["user_sk"] == user_sk
        ]
        if len(matches) > 1:
            raise RuntimeError(f"duplicate app_user user_sk: {user_sk}")
        return matches[0] if matches else None

    def replace(self, user):
        self._documents[user.identity_key] = user.to_document()
        return user