"""Stable import boundary for the one-time destructive reset utility."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "reset_legacy_engine.py"
_SPEC = spec_from_file_location("auspex_legacy_reset_implementation", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("legacy reset implementation could not be loaded")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

CONFIRMATION_TOKEN = _MODULE.CONFIRMATION_TOKEN
LegacyEngineReset = _MODULE.LegacyEngineReset
OneLakeObject = _MODULE.OneLakeObject
WarehouseObject = _MODULE.WarehouseObject
_retry_operation = _MODULE._retry_operation
build_reset_plan = _MODULE.build_reset_plan
preservation_manifest = _MODULE.preservation_manifest
require_confirmation = _MODULE.require_confirmation
warehouse_drop_statements = _MODULE.warehouse_drop_statements
