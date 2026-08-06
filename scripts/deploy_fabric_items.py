import argparse
import base64
import hashlib
import json
import time
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential


ROOT = Path(__file__).resolve().parents[1]
FABRIC_ROOT = ROOT / "fabric"
WORKSPACE_TOKEN = "00000000-0000-4000-8000-000000000001"
LAKEHOUSE_TOKEN = "00000000-0000-4000-8000-000000000002"
EDGAR_USER_AGENT_TOKEN = "{{EDGAR_USER_AGENT}}"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"
ENGINE_TARGETS = {
    ROOT / "engine" / "thesis.py": "Files/config/engine/f2359e9781c04f062a1862d8545b45d89c8b98926b66b2fa07ddaac035b86b7b.py",
    ROOT / "engine" / "fundamental_anchor.py": "Files/config/e20/84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b.py",
    ROOT / "engine" / "narrative_premium.py": "Files/config/e22/9a8314cfd0990f897992c7e26ba9c2daf060f8af7c5c0a78e0d656a3821e2b07.py",
}


def bind_definition_text(text, workspace_id, lakehouse_id, edgar_user_agent=""):
    bound = text.replace(WORKSPACE_TOKEN, workspace_id).replace(
        LAKEHOUSE_TOKEN, lakehouse_id
    )
    if EDGAR_USER_AGENT_TOKEN in bound:
        if not edgar_user_agent.strip():
            raise RuntimeError("EDGAR user agent is required for Fabric notebook deployment")
        bound = bound.replace(EDGAR_USER_AGENT_TOKEN, edgar_user_agent)
    return bound


class FabricItemDeployer:
    def __init__(
        self,
        workspace_id,
        lakehouse_id,
        edgar_user_agent,
        credential=None,
        http_client=None,
    ):
        self.workspace_id = workspace_id
        self.lakehouse_id = lakehouse_id
        self.edgar_user_agent = edgar_user_agent
        self.credential = credential or DefaultAzureCredential()
        self.http = http_client or httpx.Client(timeout=60)
        self.base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"

    def _headers(self, scope):
        return {
            "Authorization": f"Bearer {self.credential.get_token(scope).token}",
            "Content-Type": "application/json",
        }

    def _items(self):
        response = self.http.get(
            f"{self.base_url}/items", headers=self._headers(FABRIC_SCOPE)
        )
        response.raise_for_status()
        return response.json().get("value", [])

    def _parts(self, item_path, allowed_names=None):
        parts = []
        for path in sorted(candidate for candidate in item_path.rglob("*") if candidate.is_file()):
            if allowed_names and path.name not in allowed_names:
                continue
            relative_path = path.relative_to(item_path).as_posix()
            text = bind_definition_text(
                path.read_text(encoding="utf-8"),
                self.workspace_id,
                self.lakehouse_id,
                self.edgar_user_agent,
            )
            parts.append(
                {
                    "path": relative_path,
                    "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "payloadType": "InlineBase64",
                }
            )
        return parts

    def _upsert(self, display_name, item_type, parts, items):
        matches = [
            item
            for item in items
            if item.get("displayName") == display_name and item.get("type") == item_type
        ]
        if len(matches) > 1:
            raise RuntimeError(f"Multiple {item_type} items named {display_name} exist")
        definition = {"parts": parts}
        if matches:
            item_id = matches[0]["id"]
            response = self.http.post(
                f"{self.base_url}/items/{item_id}/updateDefinition?updateMetadata=true",
                headers=self._headers(FABRIC_SCOPE),
                json={"definition": definition},
            )
            action = "updated"
        else:
            response = self.http.post(
                f"{self.base_url}/items",
                headers=self._headers(FABRIC_SCOPE),
                json={
                    "displayName": display_name,
                    "type": item_type,
                    "definition": definition,
                },
            )
            item_id = None
            action = "created"
        response.raise_for_status()
        self._wait_for_operation(response)
        return {"display_name": display_name, "item_id": item_id, "action": action}

    def _wait_for_operation(self, response):
        if response.status_code != 202:
            try:
                return response.json()
            except ValueError:
                return None
        location = response.headers.get("Location") or response.headers.get("Operation-Location")
        if not location:
            raise RuntimeError("Fabric accepted an item deployment without an operation URL")
        while True:
            result = self.http.get(location, headers=self._headers(FABRIC_SCOPE))
            result.raise_for_status()
            operation = result.json()
            status = str(operation.get("status") or "").lower()
            if status in {"succeeded", "completed"}:
                operation_result = self.http.get(
                    f"{location.rstrip('/')}/result",
                    headers=self._headers(FABRIC_SCOPE),
                )
                if operation_result.status_code < 400:
                    try:
                        return operation_result.json()
                    except ValueError:
                        pass
                return operation
            if status in {"failed", "cancelled"}:
                raise RuntimeError(f"Fabric item deployment {status}: {result.text}")
            time.sleep(int(result.headers.get("Retry-After", "2")))

    def _upload_engine(self, source_path, target_path):
        content = source_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        if digest != Path(target_path).stem:
            raise RuntimeError(f"Content-addressed engine path mismatch for {source_path.name}")
        base_url = (
            "https://onelake.dfs.fabric.microsoft.com/"
            f"{self.workspace_id}/{self.lakehouse_id}"
        )
        headers = self._headers(STORAGE_SCOPE)
        headers["x-ms-version"] = "2023-11-03"
        target_url = f"{base_url}/{target_path}"
        existing = self.http.get(target_url, headers=headers)
        if existing.status_code == 200:
            if hashlib.sha256(existing.content).hexdigest() != digest:
                raise RuntimeError(f"Immutable engine conflict at {target_path}")
            return {"path": target_path, "sha256": digest, "action": "verified"}
        if existing.status_code != 404:
            existing.raise_for_status()
        for directory in ("Files/config", str(Path(target_path).parent).replace("\\", "/")):
            response = self.http.put(
                f"{base_url}/{directory}", headers=headers, params={"resource": "directory"}
            )
            if response.status_code != 409:
                response.raise_for_status()
        response = self.http.put(target_url, headers=headers, params={"resource": "file"})
        response.raise_for_status()
        response = self.http.patch(
            target_url,
            headers=headers,
            params={"action": "append", "position": "0"},
            content=content,
        )
        response.raise_for_status()
        response = self.http.patch(
            target_url,
            headers=headers,
            params={"action": "flush", "position": str(len(content))},
        )
        response.raise_for_status()
        return {"path": target_path, "sha256": digest, "action": "created"}

    def _refresh_ontology_graph(self, ontology_id):
        graph_name = f"auspex_iq_pilot_graph_{ontology_id.replace('-', '')}"
        response = self.http.get(
            f"{self.base_url}/GraphModels", headers=self._headers(FABRIC_SCOPE)
        )
        response.raise_for_status()
        matches = [
            graph
            for graph in response.json().get("value", [])
            if graph.get("displayName") == graph_name
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected one managed Graph model named {graph_name}, found {len(matches)}"
            )
        graph = matches[0]
        graph_url = f"{self.base_url}/GraphModels/{graph['id']}"
        response = self.http.post(
            f"{graph_url}/getDefinition", headers=self._headers(FABRIC_SCOPE)
        )
        response.raise_for_status()
        definition_result = self._wait_for_operation(response) or {}
        definition = definition_result.get("definition", {})
        parts = [
            part for part in definition.get("parts", []) if part.get("path") != ".platform"
        ]
        if len(parts) < 5:
            raise RuntimeError(f"Managed Graph definition is incomplete: parts={len(parts)}")

        response = self.http.get(
            f"{graph_url}/jobs/instances?jobType=Refresh",
            headers=self._headers(FABRIC_SCOPE),
        )
        response.raise_for_status()
        known_job_ids = {
            job.get("id") for job in response.json().get("value", []) if job.get("id")
        }
        response = self.http.post(
            f"{graph_url}/updateDefinition",
            headers=self._headers(FABRIC_SCOPE),
            json={"definition": {"parts": parts}},
        )
        response.raise_for_status()
        self._wait_for_operation(response)

        refresh_job = None
        for _ in range(30):
            response = self.http.get(
                f"{graph_url}/jobs/instances?jobType=Refresh",
                headers=self._headers(FABRIC_SCOPE),
            )
            response.raise_for_status()
            refresh_job = next(
                (
                    job
                    for job in response.json().get("value", [])
                    if job.get("id") not in known_job_ids
                ),
                None,
            )
            if refresh_job is not None:
                break
            time.sleep(2)
        if refresh_job is None:
            raise RuntimeError("Managed Graph save did not create a refresh job")
        return {
            "graph_id": graph["id"],
            "graph_name": graph_name,
            "query_readiness": graph.get("properties", {}).get("queryReadiness"),
            "refresh_job": refresh_job,
        }

    def deploy(self, *, include_ontology=True):
        engines = [
            self._upload_engine(source_path, target_path)
            for source_path, target_path in ENGINE_TARGETS.items()
        ]
        items = self._items()
        notebooks = [
            self._upsert(
                item_path.name.removesuffix(".Notebook"),
                "Notebook",
                self._parts(item_path, {".platform", "notebook-content.py"}),
                items,
            )
            for item_path in sorted(FABRIC_ROOT.glob("*.Notebook"))
        ]
        ontology = None
        graph = None
        if include_ontology:
            ontology_path = FABRIC_ROOT / "auspex_iq_pilot.Ontology"
            ontology = self._upsert(
                "auspex_iq_pilot",
                "Ontology",
                self._parts(ontology_path),
                items,
            )
            refreshed_items = self._items()
            ontology_matches = [
                item
                for item in refreshed_items
                if item.get("displayName") == "auspex_iq_pilot"
                and item.get("type") == "Ontology"
            ]
            if len(ontology_matches) != 1:
                raise RuntimeError(
                    "Expected one deployed Ontology named auspex_iq_pilot before graph refresh"
                )
            graph = self._refresh_ontology_graph(ontology_matches[0]["id"])
        return {
            "engines": engines,
            "notebooks": notebooks,
            "ontology": ontology,
            "graph": graph,
        }


def main():
    parser = argparse.ArgumentParser(description="Bind and deploy portable Auspex Fabric items")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--lakehouse-id", required=True)
    parser.add_argument("--edgar-user-agent", required=True)
    parser.add_argument("--skip-ontology", action="store_true")
    args = parser.parse_args()
    result = FabricItemDeployer(
        args.workspace_id,
        args.lakehouse_id,
        args.edgar_user_agent,
    ).deploy(
        include_ontology=not args.skip_ontology
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()