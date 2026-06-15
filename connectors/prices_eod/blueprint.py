import json
import uuid

import azure.functions as func

from shared.clients import get_bronze_writer, get_control_plane
from shared.models import RunContext

from .connector import PricesEodConnector

bp = func.Blueprint()


@bp.route(route="prices_eod/run", methods=["POST"])
def prices_eod_run(req: func.HttpRequest) -> func.HttpResponse:
    body = req.get_json() if req.get_body() else {}
    run_id = body.get("run_id") or f"prices_eod-{uuid.uuid4().hex[:12]}"
    symbols = body.get("symbols", [])

    if not symbols:
        return func.HttpResponse(
            json.dumps({"error": "'symbols' list is required"}),
            mimetype="application/json",
            status_code=400,
        )

    ctx = RunContext(run_id=run_id, source_id="prices_eod")

    try:
        connector = PricesEodConnector(get_control_plane(), get_bronze_writer(), symbols=symbols)
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
