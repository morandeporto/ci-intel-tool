"""Load YAML configuration files for the CI Intel tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "ci_intel.db"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected mapping in {path}, got {type(data).__name__}")
    return data


def load_competitors(config_dir: Path | None = None) -> list[dict[str, Any]]:
    base = config_dir or CONFIG_DIR
    data = _load_yaml(base / "competitors.yaml")
    competitors = data.get("competitors", [])
    if not isinstance(competitors, list):
        raise ConfigError("competitors.yaml must contain a competitors list")
    return competitors


def load_sources(config_dir: Path | None = None) -> list[dict[str, Any]]:
    base = config_dir or CONFIG_DIR
    data = _load_yaml(base / "sources.yaml")
    sources = data.get("sources", [])
    if not isinstance(sources, list):
        raise ConfigError("sources.yaml must contain a sources list")
    return sources


def load_weights(config_dir: Path | None = None) -> dict[str, float]:
    """Return dimension_name -> weight. Weights must sum to ~1.0."""
    base = config_dir or CONFIG_DIR
    data = _load_yaml(base / "weights.yaml")
    dimensions = data.get("dimensions", {})
    if not isinstance(dimensions, dict) or not dimensions:
        raise ConfigError("weights.yaml must contain a dimensions mapping")
    weights: dict[str, float] = {}
    for name, meta in dimensions.items():
        if isinstance(meta, dict):
            weights[name] = float(meta["weight"])
        else:
            weights[name] = float(meta)
    total = sum(weights.values())
    if abs(total - 1.0) > 0.01:
        raise ConfigError(f"weights must sum to 1.0, got {total:.4f}")
    return weights


def load_model_config(config_dir: Path | None = None) -> dict[str, Any]:
    base = config_dir or CONFIG_DIR
    data = _load_yaml(base / "model.yaml")
    # pipeline_model is canonical, model_id kept as alias for older callers/tests.
    if "pipeline_model" not in data and "model_id" in data:
        data["pipeline_model"] = data["model_id"]
    if "model_id" not in data and "pipeline_model" in data:
        data["model_id"] = data["pipeline_model"]
    # provider is optional / informational (SDK is Gemini-specific today).
    required = (
        "model_id",
        "max_items_per_run",
        "max_per_source",
        "window_hours",
        "fetch_timeout_seconds",
        "fetch_concurrency",
        "max_excerpt_chars",
    )
    for key in required:
        if key not in data:
            raise ConfigError(f"model.yaml missing required key: {key}")
    selection = data.get("selection") or {}
    reserved = selection.get("reserved_slots") or {}
    for slot_key in ("official_competitor", "emerging", "industry_community"):
        if slot_key not in reserved:
            raise ConfigError(
                f"model.yaml selection.reserved_slots missing key: {slot_key}"
            )
    # Soft budget sanity check (warn only - never hard-fail config load).
    try:
        from src.process.llm_quota import warn_if_budgets_exceed_limits

        warn_if_budgets_exceed_limits(data)
    except Exception:  # noqa: BLE001
        pass
    return data


def load_relevance_config(config_dir: Path | None = None) -> dict[str, Any]:
    """Keyword gate lists and title exclude patterns from relevance.yaml."""
    base = config_dir or CONFIG_DIR
    data = _load_yaml(base / "relevance.yaml")
    for key in ("strong_keywords", "exclude_title_patterns"):
        if key not in data or not isinstance(data[key], list):
            raise ConfigError(f"relevance.yaml must contain a list for {key}")
    return data


def load_comparison(config_dir: Path | None = None) -> dict[str, Any]:
    base = config_dir or CONFIG_DIR
    return _load_yaml(base / "comparison.yaml")


_VALID_KINDS = frozenset(
    {"official_competitor", "emerging", "industry", "community"}
)
_VALID_GATES = frozenset({"off", "strict"})


def enabled_sources(
    sources: list[dict[str, Any]] | None = None,
    competitors: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return enabled sources whose competitor is enabled, null, or legacy 'industry'."""
    sources = sources if sources is not None else load_sources()
    competitors = competitors if competitors is not None else load_competitors()
    enabled_ids = {c["id"] for c in competitors if c.get("enabled", False)}
    result: list[dict[str, Any]] = []
    for source in sources:
        if not source.get("enabled", False):
            continue
        kind = source.get("kind")
        gate = source.get("gate")
        # YAML 1.1 treats bare `off` as boolean false - normalize before validate.
        if gate is False:
            gate = "off"
            source["gate"] = "off"
        elif gate is True:
            raise ConfigError(
                f"source {source.get('id')!r}: gate must be \"off\" or \"strict\" "
                f"(quote strings in YAML so off is not parsed as boolean)"
            )
        if kind not in _VALID_KINDS:
            raise ConfigError(
                f"source {source.get('id')!r} has invalid kind {kind!r}, "
                f"expected one of {sorted(_VALID_KINDS)}"
            )
        if gate not in _VALID_GATES:
            raise ConfigError(
                f"source {source.get('id')!r} has invalid gate {gate!r}, "
                f"expected one of {sorted(_VALID_GATES)}"
            )
        competitor = source.get("competitor")
        # null / industry = non-vendor outlets, otherwise require an enabled competitor.
        if competitor is None or competitor == "industry" or competitor in enabled_ids:
            result.append(source)
    return result


def source_competitor_tag(source: dict[str, Any]) -> str:
    """DB/UI competitor tag: entity id, or 'industry' when the source has no vendor."""
    competitor = source.get("competitor")
    if competitor is None or competitor == "":
        return "industry"
    return str(competitor)
