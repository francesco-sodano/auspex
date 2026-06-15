import json
import uuid

import azure.functions as func

from shared.clients import get_bronze_writer, get_control_plane
from shared.models import RunContext

from .connector import SecForm4Connector

bp = func.Blueprint()


@bp.route(route="sec_form4/run", methods=["POST"])
def sec_form4_run(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_json() if req.get_body() else {}
    run_id = body.get("run_id") or f"sec_form4-{uuid.uuid4().hex[:12]}"
    ctx = RunContext(run_id=run_id, source_id="sec_form4")

    try:
        connector = SecForm4Connector(get_control_plane(), get_bronze_writer())
        result = connector.run(ctx)
    except Exception as exc:
        return func.HttpResponse(
            json.dumps({"run_id": run_id, "status": "failed", "error": str(exc)}),
            mimetype="application/json",
            status_code=500,
        )

    return func.HttpResponse(
        json.dumps({
            "run_id": run_id,
            "status": result.status,
            "records_in": result.records_in,
            "bytes_written": result.bytes_written,
            "error": result.error,
        }),
        mimetype="application/json",
        status_code=200,
    )
