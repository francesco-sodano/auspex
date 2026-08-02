"""Managed-identity REST clients for E7 Azure services."""

from collections.abc import Iterable
import json
from pathlib import Path
import time

from azure.identity import DefaultAzureCredential
import httpx


SEARCH_API_VERSION = "2024-07-01"
OPENAI_API_VERSION = "2024-10-21"


class BearerRestClient:
    def __init__(self, endpoint: str, scope: str, credential=None, timeout: float = 60.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._scope = scope
        self._credential = credential or DefaultAzureCredential()
        self._client = httpx.Client(timeout=timeout)

    def request(self, method: str, path: str, *, params=None, payload=None) -> dict:
        response = None
        for attempt in range(4):
            token = self._credential.get_token(self._scope).token
            response = self._client.request(
                method,
                f"{self._endpoint}/{path.lstrip('/')}",
                params=params,
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                break
            retry_after_ms = response.headers.get("retry-after-ms")
            retry_after = response.headers.get("Retry-After")
            if retry_after_ms and retry_after_ms.isdigit():
                delay = min(float(retry_after_ms) / 1000.0, 30.0)
            elif retry_after and retry_after.isdigit():
                delay = min(float(retry_after), 30.0)
            else:
                delay = min(float(2 ** attempt), 8.0)
            time.sleep(delay)
        if response is None:
            raise RuntimeError("Azure REST request produced no response")
        if response.is_error:
            detail = response.text.strip()
            if len(detail) > 2000:
                detail = detail[:2000] + "..."
            raise RuntimeError(
                f"Azure REST {method} {path} failed with HTTP {response.status_code}: {detail}"
            )
        return response.json() if response.content else {}


class AzureOpenAIEmbeddings:
    def __init__(self, endpoint: str, deployment: str, credential=None) -> None:
        self._deployment = deployment
        self._rest = BearerRestClient(
            endpoint,
            "https://cognitiveservices.azure.com/.default",
            credential=credential,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._rest.request(
            "POST",
            f"openai/deployments/{self._deployment}/embeddings",
            params={"api-version": OPENAI_API_VERSION},
            payload={"input": texts, "model": self._deployment},
        )
        vectors = sorted(response["data"], key=lambda item: item["index"])
        if len(vectors) != len(texts):
            raise RuntimeError("Azure OpenAI returned an incomplete embedding batch")
        return [item["embedding"] for item in vectors]


class AzureOpenAIChat:
    def __init__(self, endpoint: str, deployment: str, credential=None) -> None:
        self._deployment = deployment
        self._rest = BearerRestClient(
            endpoint,
            "https://cognitiveservices.azure.com/.default",
            credential=credential,
        )

    def complete_json(self, messages: list[dict[str, str]]) -> str:
        response = self._rest.request(
            "POST",
            f"openai/deployments/{self._deployment}/chat/completions",
            params={"api-version": OPENAI_API_VERSION},
            payload={
                "messages": messages,
                "temperature": 0,
                "seed": 42,
                "max_tokens": 500,
                "response_format": {"type": "json_object"},
            },
        )
        choices = response.get("choices") or []
        if len(choices) != 1 or not choices[0].get("message", {}).get("content"):
            raise RuntimeError("Azure OpenAI returned no chat response")
        return choices[0]["message"]["content"]


class AzureSearchRestClient:
    def __init__(self, endpoint: str, index_name: str, credential=None) -> None:
        self.index_name = index_name
        self._rest = BearerRestClient(
            endpoint,
            "https://search.azure.com/.default",
            credential=credential,
        )

    def ensure_index(self, schema: dict) -> None:
        if schema.get("name") != self.index_name:
            raise ValueError("Search schema name does not match configured index")
        self._rest.request(
            "PUT",
            f"indexes/{self.index_name}",
            params={"api-version": SEARCH_API_VERSION, "allowIndexDowntime": "true"},
            payload=schema,
        )

    def upload_documents(self, documents: Iterable[dict]) -> int:
        actions = [{"@search.action": "mergeOrUpload", **document} for document in documents]
        if not actions:
            return 0
        response = self._rest.request(
            "POST",
            f"indexes/{self.index_name}/docs/index",
            params={"api-version": SEARCH_API_VERSION},
            payload={"value": actions},
        )
        failures = [item for item in response.get("value", []) if not item.get("status")]
        if failures:
            raise RuntimeError(f"Search rejected {len(failures)} evidence documents")
        return len(actions)

    def delete_stale_generation(self, generation: str, batch_size: int = 1000) -> int:
        escaped_generation = generation.replace("'", "''")
        deleted = 0
        while True:
            response = self.search({
                "search": "*",
                "filter": f"generation ne '{escaped_generation}'",
                "select": "id",
                "top": batch_size,
            })
            stale_ids = [item["id"] for item in response.get("value", [])]
            if not stale_ids:
                return deleted
            delete_response = self._rest.request(
                "POST",
                f"indexes/{self.index_name}/docs/index",
                params={"api-version": SEARCH_API_VERSION},
                payload={
                    "value": [
                        {"@search.action": "delete", "id": document_id}
                        for document_id in stale_ids
                    ]
                },
            )
            failures = [item for item in delete_response.get("value", []) if not item.get("status")]
            if failures:
                raise RuntimeError(f"Search rejected {len(failures)} stale-document deletions")
            deleted += len(stale_ids)

    def search(self, payload: dict) -> dict:
        return self._rest.request(
            "POST",
            f"indexes/{self.index_name}/docs/search",
            params={"api-version": SEARCH_API_VERSION},
            payload=payload,
        )

    def statistics(self) -> dict:
        return self._rest.request(
            "GET",
            f"indexes/{self.index_name}/stats",
            params={"api-version": SEARCH_API_VERSION},
        )


def load_index_schema(openai_endpoint: str, embedding_deployment: str) -> dict:
    schema = json.loads(
        (Path(__file__).with_name("index_schema.json")).read_text(encoding="utf-8")
    )
    parameters = schema["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"]
    parameters["resourceUri"] = openai_endpoint.rstrip("/")
    parameters["deploymentId"] = embedding_deployment
    parameters["modelName"] = embedding_deployment
    return schema
