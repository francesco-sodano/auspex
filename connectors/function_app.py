import json
import uuid

import azure.functions as func

from prices_eod.connector import PricesEodConnector
from prices_eod.blueprint import bp as prices_eod_bp
from sec_form4.connector import SecForm4Connector
from sec_form4.blueprint import bp as sec_form4_bp
from shared.clients import get_bronze_writer, get_control_plane
from shared.models import RunContext

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
app.register_blueprint(sec_form4_bp)
app.register_blueprint(prices_eod_bp)

_CONNECTORS = {
	"sec_form4": lambda cp, bw, body, source: SecForm4Connector(cp, bw, source_config=source),
	"prices_eod": lambda cp, bw, body, source: PricesEodConnector(
		cp,
		bw,
		symbols=body.get("symbols") or None,
		since_date=body.get("since_date") or None,
		symbol_offset=body.get("symbol_offset") or 0,
		symbol_limit=body.get("symbol_limit") or None,
		source_config=source,
	),
}


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
	return func.HttpResponse(json.dumps(payload), mimetype="application/json", status_code=status_code)


@app.route(route="run", methods=["POST"])
def run_connector(req: func.HttpRequest) -> func.HttpResponse:
	body = req.get_json() if req.get_body() else {}
	source_id = body.get("source_id")
	if not source_id:
		return _json_response({"status": "failed", "error": "source_id is required"}, status_code=400)

	factory = _CONNECTORS.get(source_id)
	if factory is None:
		return _json_response({"source_id": source_id, "status": "failed", "error": "source is not implemented"}, status_code=404)

	run_id = body.get("run_id") or f"{source_id}-{uuid.uuid4().hex[:12]}"
	mode = body.get("mode") or "run"
	if mode not in {"run", "backfill"}:
		return _json_response({"run_id": run_id, "source_id": source_id, "status": "failed", "error": f"unsupported mode: {mode}"}, status_code=400)

	cp = get_control_plane()
	source = cp.get_source(source_id)
	if source is None:
		return _json_response({"run_id": run_id, "source_id": source_id, "status": "failed", "error": "source is not registered"}, status_code=404)
	if not source.get("enabled", False):
		return _json_response({"run_id": run_id, "source_id": source_id, "status": "skipped", "error": "source is disabled"})

	connector = factory(cp, get_bronze_writer(), body, source)
	result = connector.run(RunContext(run_id=run_id, source_id=source_id, mode=mode))
	return _json_response({
		"run_id": run_id,
		"source_id": source_id,
		"status": result.status,
		"records_in": result.records_in,
		"bytes_written": result.bytes_written,
		"error": result.error,
	}, status_code=500 if result.status == "failed" else 200)
