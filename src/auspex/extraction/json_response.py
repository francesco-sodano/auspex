import json
import re

_INVALID_UNICODE_ESCAPE = re.compile(r"\\u(?![0-9a-fA-F]{4})")


def load_model_json(raw_json: str) -> dict:
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        repaired = _INVALID_UNICODE_ESCAPE.sub(r"\\\\u", raw_json)
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            return json.loads(raw_json.replace("\\u", "\\\\u"), strict=False)
