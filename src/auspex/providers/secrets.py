"""Key Vault secret resolution (arc42 TC-04: API keys in Key Vault only, no
connection strings anywhere).

Provider clients call :func:`get_secret` at construction time to obtain their
API key; the key is never logged, persisted, or embedded in code/config.
Managed identity (``DefaultAzureCredential``) authenticates against Key
Vault, so no client secret is stored either.
"""

from __future__ import annotations

from functools import lru_cache

from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient


class SecretResolver:
    def __init__(self, vault_url: str) -> None:
        self._vault_url = vault_url
        self._credential: DefaultAzureCredential | None = None
        self._client: SecretClient | None = None
        self._cache: dict[str, str] = {}

    def _ensure_client(self) -> SecretClient:
        if self._client is None:
            self._credential = DefaultAzureCredential()
            self._client = SecretClient(vault_url=self._vault_url, credential=self._credential)
        return self._client

    async def get_secret(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        client = self._ensure_client()
        secret = await client.get_secret(name)
        value = secret.value
        if value is None:  # pragma: no cover - defensive
            raise LookupError(f"secret {name!r} has no value in Key Vault")
        self._cache[name] = value
        return value

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._credential is not None:
            await self._credential.close()


@lru_cache
def get_secret_resolver(vault_url: str) -> SecretResolver:
    return SecretResolver(vault_url)
