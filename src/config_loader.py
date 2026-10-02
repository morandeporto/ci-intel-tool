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
    required = ("provider", "model_id", "max_items_per_run", "max_excerpt_chars")
    for key in required:
        if key not in data:
            raise ConfigError(f"model.yaml missing required key: {key}")
    return data


def load_comparison(config_dir: Path | None = None) -> dict[str, Any]:
    base = config_dir or CONFIG_DIR
    return _load_yaml(base / "comparison.yaml")


def enabled_sources(
    sources: list[dict[str, Any]] | None = None,
    competitors: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return sources that are enabled and whose competitor is enabled (or industry)."""
    sources = sources if sources is not None else load_sources()
    competitors = competitors if competitors is not None else load_competitors()
    enabled_ids = {
        c["id"] for c in competitors if c.get("enabled", False)
    }
    enabled_ids.add("industry")  # industry outlets are always eligible
    return [
        s
        for s in sources
        if s.get("enabled", False) and s.get("competitor") in enabled_ids
    ]
