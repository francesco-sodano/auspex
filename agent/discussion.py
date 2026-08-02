from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re


DISCUSSION_PROMPT_VERSION = "e18_discussion_v1"
_MONEY = Decimal("0.01")
_WEIGHT = Decimal("0.00000001")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_AMOUNT_PATTERN = re.compile(
    r"\b(?:add|invest)\s+(?P<amount>\d+(?:\.\d+)?)(?P<suffix>[kK]?)"
    r"(?:\s+(?P<currency>USD|CHF|EUR))?\s+(?:to|in)\s+(?P<ticker>[A-Z][A-Z0-9.]{0,7})\b"
)
_UPPERCASE_PATTERN = re.compile(r"\b[A-Z][A-Z0-9.]{1,7}\b")
_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")
_ALLOWED_TERMS = {
    "ADD", "AI", "BUY", "CHF", "ETF", "EUR", "GBP", "HOLD", "SEC",
    "SELL", "TRIM", "USD",
}
_PROHIBITED_CLAIMS = (
    "guaranteed return", "guaranteed profit", "risk-free", "order placed",
    "i executed", "trade executed",
)


class DiscussionGroundingViolation(ValueError):
    pass


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _numbers(value: object) -> set[str]:
    return set(_NUMBER_PATTERN.findall(json.dumps(value, sort_keys=True, default=str)))


def _context_tickers(context: dict, citations: list[dict]) -> set[str]:
    tickers = {
        str(holding.get("ticker")).upper()
        for holding in context.get("portfolio", {}).get("holdings", [])
        if holding.get("ticker")
    }
    tickers.update(
        str(recommendation.get("ticker")).upper()
        for recommendation in context.get("recommendations", {}).get("recommendations", [])
        if recommendation.get("ticker")
    )
    tickers.update(
        str(citation.get("symbol")).upper()
        for citation in citations
        if citation.get("symbol")
    )
    what_if = context.get("what_if") or {}
    if what_if.get("ticker"):
        tickers.add(str(what_if["ticker"]).upper())
    return tickers


def _allowed_numbers(context: dict, citations: list[dict]) -> set[str]:
    values = _numbers(context) | _numbers(citations)
    for container in (
        context.get("portfolio", {}),
        *(context.get("portfolio", {}).get("holdings", [])),
        *(context.get("recommendations", {}).get("recommendations", [])),
        context.get("what_if") or {},
    ):
        for key, value in container.items():
            if "weight" not in key or value is None:
                continue
            try:
                percentage = Decimal(str(value)) * 100
            except InvalidOperation:
                continue
            values.add(format(percentage, "f").rstrip("0").rstrip("."))
    return values


def build_amount_what_if(
    query: str,
    portfolio: dict,
    resolved_tickers: dict[str, int],
) -> dict | None:
    match = _AMOUNT_PATTERN.search(query)
    if match is None:
        return None
    ticker = match.group("ticker").upper()
    if ticker not in resolved_tickers:
        raise ValueError("what-if ticker is not a resolved security")
    base_currency = str(portfolio.get("base_currency") or "").upper()
    currency = str(match.group("currency") or base_currency).upper()
    if currency != base_currency:
        raise ValueError("what-if amount must use the portfolio base currency")
    amount = Decimal(match.group("amount"))
    if match.group("suffix"):
        amount *= 1000
    if amount <= 0 or amount > Decimal("1000000000"):
        raise ValueError("what-if amount is outside the supported range")
    total_value = Decimal(str(portfolio.get("total_value_base") or "0"))
    if total_value <= 0:
        raise ValueError("portfolio value is required for a what-if")
    holding = next(
        (
            row for row in portfolio.get("holdings", [])
            if str(row.get("ticker") or "").upper() == ticker
        ),
        {},
    )
    current_value = Decimal(str(holding.get("market_value_base") or "0"))
    projected_total = total_value + amount
    projected_position = current_value + amount
    return {
        "ticker": ticker,
        "security_sk": resolved_tickers[ticker],
        "amount_input": f"{match.group('amount')}{match.group('suffix') or ''}",
        "amount_base": str(amount.quantize(_MONEY, rounding=ROUND_HALF_UP)),
        "base_currency": base_currency,
        "current_total_value_base": str(total_value.quantize(_MONEY)),
        "current_position_value_base": str(current_value.quantize(_MONEY)),
        "projected_total_value_base": str(projected_total.quantize(_MONEY)),
        "projected_position_value_base": str(projected_position.quantize(_MONEY)),
        "projected_weight": str(
            (projected_position / projected_total).quantize(_WEIGHT, rounding=ROUND_HALF_UP)
        ),
        "assumption": "The amount is new external cash invested entirely in the named security; costs and price movement are excluded.",
    }


def validate_discussion_output(
    output: dict,
    context: dict,
    citations: list[dict],
) -> dict:
    if not isinstance(output, dict):
        raise DiscussionGroundingViolation("discussion output must be an object")
    for field in ("answer", "confidence", "limitations", "evidence_ids", "metric_keys"):
        if field not in output:
            raise DiscussionGroundingViolation(f"discussion output is missing {field}")
    answer = output["answer"]
    limitations = output["limitations"]
    confidence = output["confidence"]
    if not isinstance(answer, str) or not 1 <= len(answer) <= 2000:
        raise DiscussionGroundingViolation("answer length is invalid")
    if not isinstance(limitations, str) or not 1 <= len(limitations) <= 500:
        raise DiscussionGroundingViolation("limitations length is invalid")
    if confidence not in {"high", "medium", "low"}:
        raise DiscussionGroundingViolation("confidence is invalid")
    evidence_ids = output["evidence_ids"]
    metric_keys = output["metric_keys"]
    if not isinstance(evidence_ids, list) or not isinstance(metric_keys, list):
        raise DiscussionGroundingViolation("grounding references must be lists")
    known_evidence = {citation.get("id") for citation in citations}
    if any(evidence_id not in known_evidence for evidence_id in evidence_ids):
        raise DiscussionGroundingViolation("discussion cited unknown evidence")
    known_metrics = set(
        context.get("metric_values")
        or _metric_values(
            context.get("portfolio", {}),
            context.get("recommendations", {}),
            context.get("what_if"),
        )
    )
    if any(metric_key not in known_metrics for metric_key in metric_keys):
        raise DiscussionGroundingViolation("discussion cited unknown metric")
    if not evidence_ids and not metric_keys:
        raise DiscussionGroundingViolation("discussion requires metric or evidence grounding")
    narrated_numbers = _numbers({"answer": answer, "limitations": limitations})
    unsupported_numbers = narrated_numbers - _allowed_numbers(context, citations)
    if unsupported_numbers:
        raise DiscussionGroundingViolation(
            f"discussion introduced unsupported numbers: {sorted(unsupported_numbers)}"
        )
    uppercase_tokens = set(_UPPERCASE_PATTERN.findall(f"{answer} {limitations}"))
    unsupported_tickers = uppercase_tokens - _context_tickers(context, citations) - _ALLOWED_TERMS
    if unsupported_tickers:
        raise DiscussionGroundingViolation(
            f"discussion introduced unsupported ticker-like tokens: {sorted(unsupported_tickers)}"
        )
    normalized = f"{answer} {limitations}".lower()
    if any(claim in normalized for claim in _PROHIBITED_CLAIMS):
        raise DiscussionGroundingViolation("discussion contains a prohibited claim")
    return {
        "answer": answer.strip(),
        "confidence": confidence,
        "limitations": limitations.strip(),
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "metric_keys": list(dict.fromkeys(metric_keys)),
    }


class AzureOpenAIDiscussionNarrator:
    def __init__(self, chat_client, *, model_version: str) -> None:
        self._chat = chat_client
        self.model_version = model_version
        self._instructions = (
            Path(__file__).with_name("foundry_config") / "discussion_instructions.txt"
        ).read_text(encoding="utf-8")

    def discuss(self, payload: dict) -> dict:
        response = self._chat.complete_json([
            {"role": "system", "content": self._instructions},
            {"role": "user", "content": json.dumps(payload, sort_keys=True, default=str)},
        ])
        try:
            parsed = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("discussion narrator returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("discussion narrator output must be an object")
        return parsed


def _metric_values(portfolio: dict, recommendations: dict, what_if: dict | None) -> dict:
    values = {
        "portfolio_value": portfolio.get("total_value_base"),
        "cash_available": portfolio.get("total_cash_base"),
        "position_weight": [
            {"ticker": row.get("ticker"), "value": row.get("weight")}
            for row in portfolio.get("holdings", [])
        ],
        "opportunity_score": [
            {"ticker": row.get("ticker"), "value": row.get("opportunity_score")}
            for row in recommendations.get("recommendations", [])
        ],
        "target_weight": [
            {"ticker": row.get("ticker"), "value": row.get("target_weight")}
            for row in recommendations.get("recommendations", [])
        ],
        "suggested_amount": [
            {"ticker": row.get("ticker"), "value": row.get("suggested_amount_base")}
            for row in recommendations.get("recommendations", [])
        ],
        "estimated_cost": [
            {"ticker": row.get("ticker"), "value": row.get("estimated_cost_base")}
            for row in recommendations.get("recommendations", [])
        ],
    }
    if what_if is not None:
        values["what_if"] = what_if
    return values


def _evidence_snapshot(citations: list[dict]) -> list[dict]:
    fields = (
        "id", "security_sk", "symbol", "source_type", "source_id", "source_name",
        "url", "title", "excerpt", "event_date", "knowledge_date", "revision_hash",
        "content_status",
    )
    rows = []
    for citation in citations:
        row = {field: citation.get(field) for field in fields}
        url = str(row.get("url") or "")
        row["url"] = url if url.startswith("https://") else None
        rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("id") or ""))


class GroundedDiscussionService:
    def __init__(
        self, identity, portfolio, recommendations, evidence, narrator, repository, *, clock=None,
    ) -> None:
        self._identity = identity
        self._portfolio = portfolio
        self._recommendations = recommendations
        self._evidence = evidence
        self._narrator = narrator
        self._repository = repository
        self._clock = clock

    def discuss(self, principal_header, payload: dict) -> tuple[dict, bool]:
        user = self._identity.product_user(principal_header)
        if not isinstance(payload, dict) or "owner_user_sk" in payload:
            raise ValueError("discussion payload is invalid")
        conversation_id = payload.get("conversation_id")
        client_request_id = payload.get("client_request_id")
        query = payload.get("query")
        if not isinstance(conversation_id, str) or not _ID_PATTERN.fullmatch(conversation_id):
            raise ValueError("conversation_id is invalid")
        if not isinstance(client_request_id, str) or not _ID_PATTERN.fullmatch(client_request_id):
            raise ValueError("client_request_id is invalid")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ValueError("query must be between 1 and 500 characters")
        query = query.strip()
        request_hash = _canonical_hash({
            "conversation_id": conversation_id,
            "query": query,
        })
        exchange_id = _canonical_hash({
            "owner_user_sk": user.user_sk,
            "client_request_id": client_request_id,
        })
        existing = self._repository.read_exchange(user.user_sk, exchange_id)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing.public_payload(), False

        portfolio = self._portfolio.portfolio_summary(principal_header)
        if portfolio.get("status") != "ready":
            raise ValueError("discussion requires a ready portfolio")
        recommendations = self._recommendations.recommendations(principal_header)
        resolved_tickers: dict[str, int] = {}
        for row in portfolio.get("holdings", []):
            if row.get("ticker") and row.get("security_sk") is not None:
                resolved_tickers[str(row["ticker"]).upper()] = int(row["security_sk"])
        for row in recommendations.get("recommendations", []):
            if row.get("ticker") and row.get("security_sk") is not None:
                resolved_tickers[str(row["ticker"]).upper()] = int(row["security_sk"])
        mentioned = set(_UPPERCASE_PATTERN.findall(query)) - _ALLOWED_TERMS
        for ticker in sorted(mentioned):
            if ticker in resolved_tickers:
                continue
            try:
                security = self._portfolio.lookup_security(principal_header, ticker)
            except Exception as exc:
                raise ValueError(f"unknown security in discussion: {ticker}") from exc
            resolved_tickers[ticker] = int(security.security_sk)
        what_if = build_amount_what_if(query, portfolio, resolved_tickers)
        as_of_text = recommendations.get("as_of") or portfolio.get("valuation_as_of")
        try:
            as_of = date.fromisoformat(as_of_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("discussion as_of is invalid") from exc
        citations = _evidence_snapshot(self._evidence.retrieve(
            query=query,
            as_of=as_of,
            security_sks=[resolved_tickers[ticker] for ticker in sorted(mentioned)],
            limit=10,
        ))
        profile = self._repository.get_advisor_profile(user.user_sk, user.risk_profile)
        history = self._repository.list_exchanges(user.user_sk, conversation_id, limit=10)
        context = {
            "portfolio": portfolio,
            "recommendations": recommendations,
            "what_if": what_if,
        }
        context["metric_values"] = _metric_values(portfolio, recommendations, what_if)
        narrator_payload = {
            "task": "Answer the user's question using only supplied metrics and evidence.",
            "output_schema": {
                "answer": "plain-language grounded answer",
                "confidence": "high, medium, or low",
                "limitations": "explicit uncertainty",
                "evidence_ids": ["supplied evidence IDs only"],
                "metric_keys": ["supplied metric keys only"],
            },
            "advisor_preferences": profile["instructions"],
            "untrusted_user_query": query,
            "recent_history": [row.context_payload() for row in history],
            "context": context,
            "untrusted_evidence": citations,
        }
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        reasons: list[str] = []
        status = "withheld"
        output = {}
        try:
            output = validate_discussion_output(
                self._narrator.discuss(narrator_payload), context, citations
            )
            status = "published"
        except Exception:
            reasons.append("discussion_grounding_failed")
        cited_ids = set(output.get("evidence_ids") or [])
        cited_evidence = [row for row in citations if row.get("id") in cited_ids]
        input_snapshot_hash = _canonical_hash(narrator_payload)
        exchange = self._repository.exchange_type(
            exchange_id=exchange_id,
            owner_user_sk=user.user_sk,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            request_hash=request_hash,
            query=query,
            status=status,
            answer=output.get("answer") or "",
            confidence=output.get("confidence") or "low",
            limitations=output.get("limitations") or "A grounded answer could not be produced.",
            evidence_pack=cited_evidence if status == "published" else [],
            metric_keys=tuple(output.get("metric_keys") or ()),
            what_if=what_if,
            input_snapshot_hash=input_snapshot_hash,
            model_version=self._narrator.model_version,
            prompt_version=DISCUSSION_PROMPT_VERSION,
            reasons=tuple(reasons),
            created_at=now.isoformat(),
        )
        stored, created = self._repository.append_exchange(user.user_sk, exchange)
        return stored.public_payload(), created

    def history(self, principal_header, conversation_id: str) -> list[dict]:
        user = self._identity.product_user(principal_header)
        if not isinstance(conversation_id, str) or not _ID_PATTERN.fullmatch(conversation_id):
            raise ValueError("conversation_id is invalid")
        return [
            exchange.public_payload()
            for exchange in self._repository.list_exchanges(
                user.user_sk, conversation_id, limit=100
            )
        ]

    def advisor_profile(self, principal_header) -> dict:
        user = self._identity.product_user(principal_header)
        return self._repository.get_advisor_profile(user.user_sk, user.risk_profile)

    def update_advisor_profile(self, principal_header, payload: dict) -> dict:
        user = self._identity.product_user(principal_header)
        if not isinstance(payload, dict) or "owner_user_sk" in payload:
            raise ValueError("advisor profile payload is invalid")
        instructions = payload.get("instructions")
        if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 1000:
            raise ValueError("advisor instructions must be between 1 and 1000 characters")
        normalized = instructions.strip()
        blocked = ("ignore safety", "ignore system", "guarantee profit", "execute trades")
        if any(phrase in normalized.lower() for phrase in blocked):
            raise ValueError("advisor instructions cannot override immutable safety rules")
        return self._repository.save_advisor_profile(
            user.user_sk, user.risk_profile, normalized
        )

    def reset_advisor_profile(self, principal_header) -> dict:
        user = self._identity.product_user(principal_header)
        return self._repository.reset_advisor_profile(user.user_sk, user.risk_profile)