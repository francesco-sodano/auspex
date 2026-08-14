"""Bootstrap CLI (arc42 §6.3) and weekly performance CLI (arc42 §5.8), plus
the `auspex` console entrypoint (arc42 §6.1, §7)."""

from __future__ import annotations

from auspex.cli.bootstrap import BootstrapReport, BootstrapRunner
from auspex.cli.main import main

__all__ = ["BootstrapReport", "BootstrapRunner", "main"]
