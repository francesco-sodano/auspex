"""Durable orchestration for the standalone company opportunity engine."""


def company_engine_orchestrator(context, payload=None):
    payload = payload or context.get_input() or {}
    result = yield context.call_activity("refresh_company_engine", payload)
    return result
