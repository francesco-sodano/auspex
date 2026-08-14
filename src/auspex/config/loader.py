"""Config loading and versioning.

Loads the YAML files in ``config/`` into typed structures, builds a stable
fingerprint of the whole scoring-relevant bundle, and produces a
:class:`~auspex.models.config_version.ConfigVersion` suitable for writing to
the ``config_versions`` container so every historical score can cite exactly
the parameters used to compute it (arc42 §5.11).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from auspex.models.config_version import ConfigVersion
from auspex.models.enums import FilerProfile
from auspex.models.security import Security
from auspex.settings import get_settings


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _to_decimal_tree(node):
    """Recursively convert YAML string leaves that look numeric-config into Decimal.

    We keep everything as strings on disk (git-diff friendly, no float leakage)
    and only decimalise on load, immediately before use in arithmetic.
    """

    if isinstance(node, dict):
        return {k: _to_decimal_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_to_decimal_tree(v) for v in node]
    return node


@dataclass(frozen=True)
class Universe:
    securities: list[Security]

    def by_ticker(self) -> dict[str, Security]:
        return {s.ticker: s for s in self.securities}

    def by_id(self) -> dict[str, Security]:
        return {s.id: s for s in self.securities}

    def cohort_members(self, cohort: str) -> list[Security]:
        return [s for s in self.securities if s.cohort == cohort]


def _security_id_for_ticker(ticker: str) -> str:
    """Deterministic uuid5-derived id so re-loading config never changes ids."""

    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"auspex.security.{ticker}"))


@lru_cache
def load_universe(config_dir: Path | None = None) -> Universe:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    raw = _load_yaml(config_dir / "universe.yaml")
    exchange_path = config_dir / "exchanges.yaml"
    exchanges = (
        _load_yaml(exchange_path).get("exchanges", {})
        if exchange_path.exists()
        else {}
    )
    securities = []
    for row in raw["securities"]:
        securities.append(
            Security(
                id=_security_id_for_ticker(row["ticker"]),
                ticker=row["ticker"],
                cik=row["cik"],
                name=row["name"],
                exchange=exchanges.get(row["ticker"], "US"),
                cohort=row["cohort"],
                filer_profile=FilerProfile(row["filer_profile"]),
                investable=row.get("investable", True),
            )
        )
    return Universe(securities=securities)


@lru_cache
def load_cohorts(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "cohorts.yaml")


@lru_cache
def load_weights(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "weights.yaml")


@lru_cache
def load_policy(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "policy.yaml")


@lru_cache
def load_xbrl_concepts(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "xbrl_concepts.yaml")


@lru_cache
def load_label_mappings(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "label_mappings.yaml")


@lru_cache
def load_taxonomy(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "taxonomy.yaml")


@lru_cache
def load_fees(config_dir: Path | None = None) -> dict:
    settings = get_settings()
    config_dir = config_dir or settings.config_dir
    return _load_yaml(config_dir / "fees.yaml")


def weight_decimal(cohort_or_profile: dict, leg: str) -> Decimal:
    return Decimal(cohort_or_profile[leg])


def _fingerprint(bundle: dict) -> str:
    canonical = json.dumps(bundle, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_config_version(version_id: str, created_at, config_dir: Path | None = None) -> ConfigVersion:
    """Assemble the full versioned bundle referenced by every score row."""

    bundle = {
        "weights": load_weights(config_dir),
        "policy": load_policy(config_dir),
        "label_mappings": load_label_mappings(config_dir),
        "cohorts": load_cohorts(config_dir),
        "taxonomy": load_taxonomy(config_dir),
        "xbrl_concepts": load_xbrl_concepts(config_dir),
        "fees": load_fees(config_dir),
    }
    return ConfigVersion(
        id=version_id,
        created_at=created_at,
        fingerprint=f"sha256:{_fingerprint(bundle)}",
        **bundle,
    )
