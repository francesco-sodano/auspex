"""Stable owner-identity mapping (arc42 §5.7, §11 API).

An Entra object ID is converted to the partition key used consistently by the
API, jobs, recommendations, projections, conversations and event ledger:

    identity_key = sha256(identity_provider + "\\0" + provider_user_id)   # hex digest
    user_sk      = uuid5(USER_NAMESPACE, identity_key)

Imported ledgers may alternatively resolve `owner_user_sk` from an `app_users`
document through :mod:`auspex.portfolio.mapping`.
"""

from __future__ import annotations

import uuid

from auspex.models.common import sha256_hex

# Fixed application namespace. Changing it orphans existing owner partitions.
USER_NAMESPACE = uuid.UUID("b7301e2f-0b55-49e4-91bd-9dfdc2ae73e7")

DEFAULT_IDENTITY_PROVIDER = "aad"


def compatible_identity_key(provider_user_id: str, identity_provider: str = DEFAULT_IDENTITY_PROVIDER) -> str:
    """SHA-256 over ``identity_provider + NUL + provider_user_id``."""

    return sha256_hex(f"{identity_provider}\0{provider_user_id}")


def compatible_user_id(provider_user_id: str, identity_provider: str = DEFAULT_IDENTITY_PROVIDER) -> str:
    """Stable surrogate key for an authenticated principal.

    Used both as Auspex's own ``user_id`` (for `recommendations`,
    `portfolio_projection`, `conversations`) and as the event ledger's
    ``owner_user_sk`` partition value.
    """

    identity_key = compatible_identity_key(provider_user_id, identity_provider)
    return str(uuid.uuid5(USER_NAMESPACE, identity_key))


def resolve_owner_user_id(owner_provider_user_id: str | None, *, default: str = "owner") -> str:
    """Resolve Auspex's own canonical ``user_id`` for the single owner
    (arc42 §1: no multi-tenancy).

    If ``owner_provider_user_id`` (the owner's fixed Entra `oid`/`sub`,
    ``Settings.owner_provider_user_id`` / ``AUSPEX_OWNER_PROVIDER_USER_ID``)
    is configured, returns the stable mapping so nightly/bootstrap
    writes land under the exact same ``user_id`` the API resolves for that
    owner's authenticated requests. Otherwise falls back to ``default`` —
    available for local/test environments where the real owner identity has
    not been configured — and the caller should log that this
    fallback is in effect, since a mismatch here means the API can never see
    what the pipeline wrote.
    """

    if owner_provider_user_id:
        return compatible_user_id(owner_provider_user_id)
    return default
