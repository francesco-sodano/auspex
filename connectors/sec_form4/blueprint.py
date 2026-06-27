import json
import uuid

import azure.functions as func

from shared.clients import get_bronze_writer, get_control_plane
from shared.models import RunContext

from .connector import SecForm4Connector

bp = func.Blueprint()


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload), mimetype="application/json", status_code=status_code)


@bp.route(route="sec_form4/run", methods=["POST"])
def sec_form4_run(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_json() if req.get_body() else {}
    run_id = body.get("run_id") or f"sec_form4-{uuid.uuid4().hex[:12]}"
    mode = body.get("mode") or "run"
    ctx = RunContext(run_id=run_id, source_id="sec_form4", mode=mode)

    try:
        cp = get_control_plane()
        source = cp.get_source("sec_form4")
        if source is not None and not source.get("enabled", False):
            return _json_response({"run_id": run_id, "status": "skipped", "error": "source is disabled"})
        connector = SecForm4Connector(cp, get_bronze_writer(), source_config=source)
        result = connector.run(ctx)
    except Exception as exc:
        return _json_response({"run_id": run_id, "status": "failed", "error": str(exc)}, status_code=500)

    return _json_response(
        {
            "run_id": run_id,
            "status": result.status,
            "records_in": result.records_in,
            "bytes_written": result.bytes_written,
            "error": result.error,
        },
        status_code=500 if result.status == "failed" else 200,
    )
