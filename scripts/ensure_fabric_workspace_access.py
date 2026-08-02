import argparse
import json

import httpx
from azure.identity import DefaultAzureCredential


FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def ensure_workspace_role(workspace_id, principal_id, role, credential=None, http_client=None):
    credential = credential or DefaultAzureCredential()
    http_client = http_client or httpx.Client(timeout=60)
    headers = {
        "Authorization": f"Bearer {credential.get_token(FABRIC_SCOPE).token}",
        "Content-Type": "application/json",
    }
    base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
    response = http_client.get(f"{base_url}/roleAssignments", headers=headers)
    response.raise_for_status()
    assignments = response.json().get("value", [])
    matches = [
        assignment
        for assignment in assignments
        if assignment.get("principal", {}).get("id") == principal_id
    ]
    if matches:
        if any(assignment.get("role") == role for assignment in matches):
            return {"status": "unchanged", "role": role}
        raise RuntimeError(
            f"Principal {principal_id} already has a different Fabric workspace role"
        )
    response = http_client.post(
        f"{base_url}/roleAssignments",
        headers=headers,
        json={
            "principal": {"id": principal_id, "type": "ServicePrincipal"},
            "role": role,
        },
    )
    response.raise_for_status()
    return {"status": "created", "role": role}


def main():
    parser = argparse.ArgumentParser(
        description="Ensure a managed identity can operate an Auspex Fabric workspace"
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--role", default="Contributor")
    args = parser.parse_args()
    result = ensure_workspace_role(args.workspace_id, args.principal_id, args.role)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()