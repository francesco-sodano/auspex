"""Standalone fresh-data company opportunity refresh service."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path

from engine.company_package import CompanySourceCursor, EvidenceRef, package_fingerprint
from engine.fresh_opportunity import FreshCompanySignal, score_fresh_theme


UNIVERSE_PATH = Path(__file__).with_name("research_universe.json")


class CompanyEngineService:
    def __init__(self, control_plane, provider, universe_path: Path = UNIVERSE_PATH) -> None:
        self.cp = control_plane
        self.provider = provider
        self.universe_path = universe_path

    def refresh(self, as_of: date) -> dict:
        universe = json.loads(self.universe_path.read_text(encoding="utf-8"))
        companies = [dict(company) for company in universe["companies"]]
        self._apply_held_security_ids(companies)
        packets = [self.provider.fetch_company(company, as_of) for company in companies]
        signals_by_theme = defaultdict(list)
        packet_by_security = {}
        for packet in packets:
            signal = _signal_from_packet(packet)
            signals_by_theme[signal.theme_id].append(signal)
            packet_by_security[signal.security_sk] = packet

        packages = []
        for theme_id in sorted(signals_by_theme):
            packages.extend(score_fresh_theme(signals_by_theme[theme_id]))
        changed = 0
        for package in packages:
            previous = self.cp.get_current_company_package(package.security_sk)
            fingerprint = package_fingerprint(package)
            if previous is None or previous.get("package_fingerprint") != fingerprint:
                changed += 1
            self.cp.publish_company_package(package)
            narrative = _narrative(package)
            self.cp.attach_company_narrative(
                security_sk=package.security_sk,
                package_fingerprint=fingerprint,
                narrative=narrative,
            )
            self._publish_serving(package, packet_by_security[package.security_sk])

        fx_documents = 0
        for source, target in (("USD", "CHF"), ("USD", "EUR"), ("EUR", "CHF")):
            row = self.provider.fetch_fx(source, target)
            if row is None:
                continue
            fx_documents += 1
            self.cp.upsert_market_data({
                "id": f"fx:{row['pair']}",
                "kind": "fx_alias",
                "pair": row["pair"],
                "rate": row["rate"],
                "as_of": row["as_of"],
                "source_id": "company_engine_v1",
                "generation": as_of.isoformat(),
            })
            self.cp.upsert_market_data({
                "id": f"fx:{row['pair']}:{row['as_of']}",
                "kind": "fx",
                "pair": row["pair"],
                "rate": row["rate"],
                "as_of": row["as_of"],
                "source_id": "company_engine_v1",
                "generation": as_of.isoformat(),
            })
        return {
            "status": "completed",
            "as_of": as_of.isoformat(),
            "universe_version": universe["version"],
            "companies": len(packages),
            "changed_packages": changed,
            "themes": len(signals_by_theme),
            "fx_pairs": fx_documents,
            "ready": sum(package.coverage_status == "READY" for package in packages),
            "partial": sum(package.coverage_status == "PARTIAL" for package in packages),
            "withheld": sum(package.coverage_status == "WITHHELD" for package in packages),
        }

    def _apply_held_security_ids(self, companies: list[dict]) -> None:
        by_ticker = {
            str(transaction.get("security_code") or "").upper(): transaction.get("security_sk")
            for transaction in self.cp.list_portfolio_transactions()
            if transaction.get("security_code") and transaction.get("security_sk")
        }
        for company in companies:
            held_security_sk = by_ticker.get(str(company["ticker"]).upper())
            if held_security_sk is not None:
                company["security_sk"] = int(held_security_sk)

    def _publish_serving(self, package, packet: dict) -> None:
        security = {
            "security_sk": package.security_sk,
            "ticker": package.ticker,
            "isin": None,
            "company_name": package.company_name,
            "currency": "USD",
            "exchange": "US",
            "gics_sector": package.theme_id,
            "country": "US",
            "source_id": "company_engine_v1",
            "generation": package.as_of.isoformat(),
        }
        for document_id in (
            f"ticker:{package.ticker}",
            f"security:{package.security_sk}",
        ):
            self.cp.upsert_security_catalog({"id": document_id, **security})
        self.cp.container("ingestion_universe").upsert_item({
            "id": package.ticker,
            "symbol": package.ticker,
            "security_sk": package.security_sk,
            "currency": "USD",
            "source": "research_universe_v1",
            "active": True,
        })
        prices = packet["prices"]
        if prices:
            latest = prices[0]
            quote = {
                "id": f"quote:security:{package.security_sk}",
                "kind": "quote",
                "security_sk": package.security_sk,
                "ticker": package.ticker,
                "price": _format_number(latest["close"]),
                "currency": "USD",
                "as_of": latest["date"],
                "source_id": "company_engine_v1",
                "generation": package.as_of.isoformat(),
            }
            self.cp.upsert_market_data(quote)
            self.cp.upsert_market_data({**quote, "id": f"quote:{package.ticker}"})
            history = {
                "id": f"history:security:{package.security_sk}",
                "kind": "history",
                "security_sk": package.security_sk,
                "ticker": package.ticker,
                "currency": "USD",
                "as_of": latest["date"],
                "prices": [
                    {"date": row["date"], "price": _format_number(row["close"])}
                    for row in reversed(prices)
                    if row["close"] is not None
                ],
                "source_id": "company_engine_v1",
                "generation": package.as_of.isoformat(),
            }
            self.cp.upsert_market_data(history)
            self.cp.upsert_market_data({**history, "id": f"history:{package.ticker}"})
        score = {
            "id": f"score:security:{package.security_sk}",
            "kind": "company_opportunity",
            "security_sk": package.security_sk,
            "ticker": package.ticker,
            "theme_id": package.theme_id,
            "as_of": package.as_of.isoformat(),
            "opportunity_score": package.opportunity_score,
            "opportunity_score_raw": package.opportunity_score_raw,
            "coverage_status": package.coverage_status,
            "coverage_reasons": list(package.coverage_reasons),
            "candidate_count": package.candidate_count,
            "score_model_version": package.model_version,
            "score_weight_version": package.weight_version,
            "source_id": "company_engine_v1",
            "generation": package.as_of.isoformat(),
        }
        self.cp.upsert_market_data(score)
        self.cp.upsert_market_data({
            "id": f"classification:security:{package.security_sk}",
            "kind": "theme_classification",
            "security_sk": package.security_sk,
            "ticker": package.ticker,
            "theme_id": package.theme_id,
            "provenance": package.classification_provenance,
            "confidence": "1.0",
            "rationale": "Curated research universe classification.",
            "classification_version": "research_universe_v1",
            "as_of": package.as_of.isoformat(),
            "source_id": "company_engine_v1",
            "generation": package.as_of.isoformat(),
        })


def _signal_from_packet(packet: dict) -> FreshCompanySignal:
    company = packet["company"]
    as_of = date.fromisoformat(packet["as_of"])
    overview = packet["overview"]
    prices = packet["prices"]
    news = packet["news"]
    insiders = packet["insider_transactions"]
    evidence = {
        "classification": _evidence(
            company,
            as_of,
            "classification",
            {"theme_id": company["theme_id"], "keywords": company["keywords"]},
            f"Curated theme classification: {company['theme_id']}.",
        ),
        "overview": _evidence(
            company,
            as_of,
            "overview",
            overview,
            _overview_excerpt(overview),
        ),
        "prices": _evidence(
            company,
            as_of,
            "prices",
            prices,
            f"Latest compact market window contains {len(prices)} sessions.",
        ),
        "news": _evidence(
            company,
            as_of,
            "news",
            news,
            _news_excerpt(news),
        ),
        "insiders": _evidence(
            company,
            as_of,
            "insider_transactions",
            insiders,
            f"Fresh 90-day insider packet contains {len(insiders)} transactions.",
        ),
    }
    raw_values = {
        "thesis_linkage": _thesis_value(overview, company["keywords"]),
        "attention_acceleration": _attention_value(news, as_of),
        "smart_money": _smart_money_value(insiders),
        "fundamental_health": _fundamental_value(overview),
        "valuation_brake": _valuation_value(overview),
        "crowding_positioning": _crowding_value(prices),
    }
    evidence_map = {
        "thesis_linkage": (evidence["classification"], evidence["overview"]),
        "attention_acceleration": (evidence["news"],),
        "smart_money": (evidence["insiders"],) if raw_values["smart_money"] is not None else (),
        "fundamental_health": (evidence["overview"],) if raw_values["fundamental_health"] is not None else (),
        "valuation_brake": (evidence["overview"], evidence["prices"]) if raw_values["valuation_brake"] is not None else (),
        "crowding_positioning": (evidence["prices"],) if raw_values["crowding_positioning"] is not None else (),
    }
    reasons = {
        leg_name: () if value is not None else (f"missing:{leg_name}_fresh_data",)
        for leg_name, value in raw_values.items()
    }
    cursors = tuple(
        CompanySourceCursor(
            source_class=source_class,
            source_id=f"company_engine:{source_class}",
            latest_record_id=reference.evidence_id,
            latest_revision_hash=reference.revision_hash,
            latest_knowledge_date=reference.knowledge_date,
        )
        for source_class, reference in sorted(evidence.items())
    )
    return FreshCompanySignal(
        security_sk=int(company["security_sk"]),
        ticker=company["ticker"],
        company_name=company["company_name"],
        as_of=as_of,
        theme_id=company["theme_id"],
        classification_provenance="curated_v1",
        classification_id=f"research_universe_v1:{company['ticker']}",
        raw_leg_values=raw_values,
        leg_evidence=evidence_map,
        leg_coverage_reasons=reasons,
        source_cursors=cursors,
    )


def _evidence(company, as_of, source_class, payload, excerpt):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return EvidenceRef(
        evidence_id=f"{source_class}:{company['ticker']}:{digest[:24]}",
        source_type=source_class,
        source_id=f"company_engine:{source_class}:{company['ticker']}",
        revision_hash=digest,
        event_date=as_of,
        knowledge_date=as_of,
        retention_class="company_package",
        excerpt=excerpt[:600] if excerpt else f"No fresh {source_class} records.",
    )


def _thesis_value(overview, keywords):
    description = str(overview.get("Description") or "").lower()
    if not description:
        return 0.5
    return sum(keyword.lower() in description for keyword in keywords) / max(1, len(keywords))


def _attention_value(news, as_of):
    current_start = as_of.fromordinal(as_of.toordinal() - 29)
    previous_start = as_of.fromordinal(as_of.toordinal() - 59)
    current = sum(date.fromisoformat(row["date"]) >= current_start for row in news)
    previous = sum(
        previous_start <= date.fromisoformat(row["date"]) < current_start
        for row in news
    )
    return math.log((current + 1.0) / (previous + 1.0))


def _smart_money_value(rows):
    if not rows:
        return None
    acquired = disposed = 0.0
    for row in rows:
        shares = float(row.get("shares") or 0)
        marker = str(row.get("acquisition_or_disposal") or "").upper()
        if marker.startswith("A") or "ACQUIS" in marker or "BUY" in marker:
            acquired += shares
        elif marker.startswith("D") or "DISPOS" in marker or "SELL" in marker:
            disposed += shares
    total = acquired + disposed
    return None if total <= 0 else (acquired - disposed) / total


def _fundamental_value(overview):
    values = [
        _ratio(overview.get("ProfitMargin")),
        _ratio(overview.get("OperatingMarginTTM")),
        _ratio(overview.get("QuarterlyRevenueGrowthYOY")),
        _ratio(overview.get("ReturnOnEquityTTM")),
    ]
    observed = [value for value in values if value is not None]
    return sum(observed) / len(observed) if len(observed) >= 2 else None


def _valuation_value(overview):
    ratios = [
        _positive(overview.get("PERatio")),
        _positive(overview.get("PEGRatio")),
        _positive(overview.get("PriceToSalesRatioTTM")),
        _positive(overview.get("EVToEBITDA")),
    ]
    yields = [1.0 / value for value in ratios if value is not None and value > 0]
    return sum(yields) / len(yields) if yields else None


def _crowding_value(prices):
    ordered = list(reversed([row for row in prices if row.get("close") and row.get("volume")]))
    if len(ordered) < 20:
        return None
    latest = ordered[-10:]
    prior = ordered[-20:-10]
    latest_volume = sum(row["volume"] for row in latest) / len(latest)
    prior_volume = sum(row["volume"] for row in prior) / len(prior)
    volume_acceleration = latest_volume / prior_volume - 1 if prior_volume > 0 else 0
    returns = [
        latest[index]["close"] / latest[index - 1]["close"] - 1
        for index in range(1, len(latest))
        if latest[index - 1]["close"] > 0
    ]
    volatility = math.sqrt(sum(value * value for value in returns) / len(returns)) if returns else 0
    return -(abs(volume_acceleration) + volatility)


def _overview_excerpt(overview):
    description = str(overview.get("Description") or "").strip()
    metrics = (
        f"Revenue growth {overview.get('QuarterlyRevenueGrowthYOY')}; "
        f"profit margin {overview.get('ProfitMargin')}; PE {overview.get('PERatio')}."
    )
    return f"{description[:400]} {metrics}".strip()


def _news_excerpt(news):
    if not news:
        return "No company news was returned in the current 60-day window."
    return " | ".join(
        str(row.get("headline") or row.get("summary") or "")[:180]
        for row in news[:3]
    )


def _narrative(package):
    raised = [leg for leg in package.legs if leg.direction == "RAISED"]
    lowered = [leg for leg in package.legs if leg.direction == "LOWERED"]
    raised.sort(key=lambda leg: abs(leg.contribution or 0), reverse=True)
    lowered.sort(key=lambda leg: abs(leg.contribution or 0), reverse=True)
    evidence = {row.evidence_id: row for row in package.evidence}
    citation_ids = tuple(
        dict.fromkeys(
            evidence_id
            for leg in (*raised[:2], *lowered[:2])
            for evidence_id in leg.evidence_ids
        )
    )
    citations = [
        {
            "evidence_id": evidence_id,
            "source_type": evidence[evidence_id].source_type,
            "event_date": evidence[evidence_id].event_date.isoformat(),
            "knowledge_date": evidence[evidence_id].knowledge_date.isoformat(),
            "excerpt": evidence[evidence_id].excerpt,
        }
        for evidence_id in citation_ids
    ]
    positive = ", ".join(leg.leg_name.replace("_", " ") for leg in raised[:2]) or "none"
    negative = ", ".join(leg.leg_name.replace("_", " ") for leg in lowered[:2]) or "none"
    return {
        "narrative_version": "deterministic_company_outlook_v1",
        "generated_by": "deterministic",
        "outlook_direction": package.outlook_direction,
        "outlook_horizon_days": package.outlook_horizon_days,
        "summary": (
            f"The 90-day opportunity outlook is {package.outlook_direction.lower()}. "
            f"The strongest positive drivers are {positive}; the main negative drivers are {negative}."
        ),
        "uncertainty": (
            f"Coverage is {package.coverage_status.lower()} across {package.candidate_count} "
            "companies in the assigned theme cohort."
        ),
        "citation_ids": list(citation_ids),
        "citations": citations,
    }


def _ratio(value):
    if value in (None, "", "None", "-"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _positive(value):
    result = _ratio(value)
    return result if result is not None and result > 0 else None


def _format_number(value):
    return f"{float(value):.6f}" if value is not None else None
