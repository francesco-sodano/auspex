import argparse
import base64
import json
import time
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "fabric" / "pipelines" / "daily_build.json"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def _activity_parameter(value):
    if isinstance(value, str) and value.startswith("@"):
        return {"value": value, "type": "Expression"}
    return {"value": value, "type": "string"}


def build_pipeline_definition(manifest, workspace_id, notebook_ids):
    activities = []
    previous_name = None
    for entry in manifest["notebooks"]:
        dependencies = []
        if previous_name:
            dependencies.append(
                {"activity": previous_name, "dependencyConditions": ["Succeeded"]}
            )
        activities.append(
            {
                "name": entry["name"],
                "type": "TridentNotebook",
                "dependsOn": dependencies,
                "policy": {
                    "timeout": "0.12:00:00",
                    "retry": 1,
                    "retryIntervalInSeconds": 60,
                    "secureInput": False,
                    "secureOutput": False,
                },
                "typeProperties": {
                    "notebookId": notebook_ids[entry["notebook"]],
                    "workspaceId": workspace_id,
                    "parameters": {
                        name: _activity_parameter(value)
                        for name, value in entry.get("parameters", {}).items()
                    },
                },
            }
        )
        previous_name = entry["name"]
    return {
        "properties": {
            "description": manifest["description"],
            "parameters": {manifest["parameter"]: {"type": "string"}},
            "activities": activities,
        }
    }


class FabricPipelineDeployer:
    def __init__(self, workspace_id, credential=None, http_client=None):
        self.workspace_id = workspace_id
        self.credential = credential or DefaultAzureCredential()
        self.http = http_client or httpx.Client(timeout=60)
        self.base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"

    def _headers(self):
        token = self.credential.get_token(FABRIC_SCOPE).token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _items(self):
        response = self.http.get(f"{self.base_url}/items", headers=self._headers())
        response.raise_for_status()
        return response.json().get("value", [])

    def deploy(self, manifest):
        items = self._items()
        pipelines = manifest.get("pipelines") or [manifest]
        notebook_names = sorted({
            entry["notebook"]
            for pipeline in pipelines
            for entry in pipeline["notebooks"]
        })
        notebook_ids = {}
        for name in notebook_names:
            matches = [
                item
                for item in items
                if item.get("displayName") == name and item.get("type") == "Notebook"
            ]
            if len(matches) != 1:
                raise RuntimeError(f"Expected one Fabric Notebook named {name}, found {len(matches)}")
            notebook_ids[name] = matches[0]["id"]

        results = []
        for pipeline in pipelines:
            definition = build_pipeline_definition(pipeline, self.workspace_id, notebook_ids)
            payload = base64.b64encode(
                json.dumps(definition, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            body = {
                "definition": {
                    "parts": [
                        {
                            "path": "pipeline-content.json",
                            "payload": payload,
                            "payloadType": "InlineBase64",
                        }
                    ]
                }
            }
            matches = [
                item
                for item in items
                if item.get("displayName") == pipeline["display_name"]
                and item.get("type") == "DataPipeline"
            ]
            if len(matches) > 1:
                raise RuntimeError(
                    f"Expected at most one DataPipeline named {pipeline['display_name']}"
                )
            if matches:
                item_id = matches[0]["id"]
                response = self.http.post(
                    f"{self.base_url}/items/{item_id}/updateDefinition",
                    headers=self._headers(),
                    json=body,
                )
                operation = "updated"
            else:
                response = self.http.post(
                    f"{self.base_url}/items",
                    headers=self._headers(),
                    json={
                        "displayName": pipeline["display_name"],
                        "description": pipeline["description"],
                        "type": "DataPipeline",
                        **body,
                    },
                )
                item_id = None
                operation = "created"
            response.raise_for_status()
            self._wait_for_operation(response)
            results.append({
                "display_name": pipeline["display_name"],
                "status": operation,
                "item_id": item_id,
                "activities": len(pipeline["notebooks"]),
            })
        return {"pipelines": results, "activities": len(notebook_names)}

    def _wait_for_operation(self, response):
        if response.status_code != 202:
            return
        location = response.headers.get("Location") or response.headers.get("Operation-Location")
        if not location:
            raise RuntimeError("Fabric accepted the pipeline deployment without an operation URL")
        while True:
            result = self.http.get(location, headers=self._headers())
            result.raise_for_status()
            status = str(result.json().get("status") or "").lower()
            if status in {"succeeded", "completed"}:
                return
            if status in {"failed", "cancelled"}:
                raise RuntimeError(f"Fabric pipeline deployment {status}: {result.text}")
            time.sleep(int(result.headers.get("Retry-After", "2")))


def main():
    parser = argparse.ArgumentParser(description="Create or update the Auspex Fabric daily pipeline")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = FabricPipelineDeployer(args.workspace_id).deploy(manifest)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()