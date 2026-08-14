"""Unit tests for the `auspex` CLI argument parsing (arc42 §6.1, §6.3, §7).

The subcommand names must match the container commands in
``infra/modules/containerapps.bicep`` (``python -m auspex nightly`` /
``python -m auspex performance``), since those Container Apps Jobs invoke
this exact CLI via module execution.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from datetime import date

from auspex.cli.main import _aclose_unique, _build_arg_parser, _parse_date


class TestArgParser:
    def test_nightly_command_parses_with_default_date(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["nightly"])
        assert ns.command == "nightly"
        assert ns.date is None

    def test_nightly_command_parses_explicit_date(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["nightly", "--date", "2026-08-08"])
        assert ns.command == "nightly"
        assert ns.date == "2026-08-08"

    def test_performance_command_parses(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["performance"])
        assert ns.command == "performance"

    def test_bootstrap_command_parses_with_defaults(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["bootstrap"])
        assert ns.command == "bootstrap"
        # user_id is no longer an operator-supplied argument — it's resolved
        # via PortfolioAdapter.resolve_owner_user_sk() at run time (never a
        # CLI-arg/placeholder value such as the literal "owner").
        assert not hasattr(ns, "user_id")

    def test_bootstrap_recover_command_parses(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["bootstrap-recover"])
        assert ns.command == "bootstrap-recover"
        assert ns.replay_all is False

    def test_bootstrap_recover_replay_all_parses(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["bootstrap-recover", "--replay-all"])
        assert ns.command == "bootstrap-recover"
        assert ns.replay_all is True

    def test_bootstrap_audit_command_parses(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["bootstrap-audit"])
        assert ns.command == "bootstrap-audit"

    def test_seed_edgar_watermarks_command_parses(self):
        parser = _build_arg_parser()
        ns = parser.parse_args(["seed-edgar-watermarks"])
        assert ns.command == "seed-edgar-watermarks"

    def test_serve_command_defaults_to_port_8080(self):
        # Container Apps ingress targets port 8080 (infra/modules/containerapps.bicep)
        parser = _build_arg_parser()
        ns = parser.parse_args(["serve"])
        assert ns.command == "serve"
        assert ns.port == 8080
        assert ns.host == "0.0.0.0"

    def test_run_pipeline_alias_no_longer_exists(self):
        parser = _build_arg_parser()
        try:
            parser.parse_args(["run-pipeline"])
            raised = False
        except SystemExit:
            raised = True
        assert raised  # "nightly" replaced "run-pipeline" to match the IaC job command

    def test_unknown_command_exits_nonzero(self):
        parser = _build_arg_parser()
        try:
            parser.parse_args(["not-a-command"])
            raised = False
        except SystemExit as exc:
            raised = exc.code != 0
        assert raised


class TestParseDate:
    def test_none_defaults_to_today(self):
        assert _parse_date(None) is not None

    def test_explicit_iso_date(self):
        assert _parse_date("2026-08-08") == date(2026, 8, 8)


def test_aclose_unique_closes_duplicate_resource_once():
    class Resource:
        calls = 0

        async def aclose(self):
            self.calls += 1

    resource = Resource()

    asyncio.run(_aclose_unique(resource, None, object(), resource))

    assert resource.calls == 1


def test_main_suppresses_http_request_logs(monkeypatch):
    cli_main = importlib.import_module("auspex.cli.main")
    monkeypatch.setattr(cli_main, "_serve_command", lambda host, port: 0)

    assert cli_main.main(["serve"]) == 0
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("azure").level == logging.WARNING
