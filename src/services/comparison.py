"""Comparison matrix service - claims only from config sources, never model memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.config_loader import load_comparison, load_competitors

# Known product/capability acronyms kept upper case when labels are formatted.
_ACRONYM_MAP = {
    "aspom": "ASPM",  # common typo → correct acronym
    "aspm": "ASPM",
    "sca": "SCA",
    "sbom": "SBOM",
    "sast": "SAST",
    "dast": "DAST",
    "iac": "IaC",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    "ml": "ML",
    "ai": "AI",
    "oci": "OCI",
}

_TOKEN_SPLIT_RE = re.compile(r"(\s+|/)")


def format_capability_label(label: str) -> str:
    """Format a capability label while keeping known acronyms upper case.

    Fixes typos like ``Aspom`` → ``ASPM`` and preserves ``SCA``, ``CI/CD``, etc.
    """
    text = (label or "").strip()
    if not text:
        return text
    parts: list[str] = []
    for part in _TOKEN_SPLIT_RE.split(text):
        if not part or part.isspace() or part == "/":
            parts.append(part)
            continue
        mapped = _ACRONYM_MAP.get(part.lower())
        parts.append(mapped if mapped is not None else part)
    return "".join(parts)


@dataclass(frozen=True)
class ComparisonClaim:
    company_id: str
    company_label: str
    claim: str
    source_url: str | None
    quote: str | None
    is_unknown: bool


@dataclass(frozen=True)
class ComparisonRow:
    capability_id: str
    capability_label: str
    claims: list[ComparisonClaim]


def _normalize_claim(raw: dict[str, Any] | None, company_id: str, company_label: str) -> ComparisonClaim:
    """Force Unknown when source_url is missing - never invent a claim."""
    if not raw or not isinstance(raw, dict):
        return ComparisonClaim(
            company_id=company_id,
            company_label=company_label,
            claim="Unknown",
            source_url=None,
            quote=None,
            is_unknown=True,
        )
    source_url = raw.get("source_url")
    claim_text = (raw.get("claim") or "").strip()
    quote = raw.get("quote")
    if not source_url or claim_text.lower() == "unknown" or not claim_text:
        return ComparisonClaim(
            company_id=company_id,
            company_label=company_label,
            claim="Unknown",
            source_url=None,
            quote=None,
            is_unknown=True,
        )
    return ComparisonClaim(
        company_id=company_id,
        company_label=company_label,
        claim=claim_text,
        source_url=str(source_url),
        quote=str(quote).strip() if quote else None,
        is_unknown=False,
    )


def get_comparison_matrix(
    config_dir=None,
    company_ids: list[str] | None = None,
) -> tuple[list[str], list[ComparisonRow], list[str], dict[str, str | None]]:
    """Load the comparison matrix from YAML.

    Returns:
        company_order, rows, context_notes, meta (last_reviewed, reviewed_by)
    """
    data = load_comparison(config_dir)
    competitors = load_competitors(config_dir)
    label_by_id = {c["id"]: c.get("display_name", c["id"]) for c in competitors}

    if company_ids is None:
        # Prefer enabled competitors, keep jfrog first.
        enabled = [c["id"] for c in competitors if c.get("enabled", False)]
        company_ids = enabled if enabled else list(label_by_id.keys())
        if "jfrog" in company_ids:
            company_ids = ["jfrog"] + [c for c in company_ids if c != "jfrog"]

    rows: list[ComparisonRow] = []
    for cap in data.get("capabilities", []):
        companies = cap.get("companies") or {}
        claims = [
            _normalize_claim(
                companies.get(cid),
                cid,
                label_by_id.get(cid, cid.title()),
            )
            for cid in company_ids
        ]
        rows.append(
            ComparisonRow(
                capability_id=cap.get("id", ""),
                capability_label=format_capability_label(
                    str(cap.get("label") or cap.get("id") or "")
                ),
                claims=claims,
            )
        )

    context_notes = [str(n) for n in data.get("context_notes", [])]
    meta = {
        "last_reviewed": data.get("last_reviewed"),
        "reviewed_by": data.get("reviewed_by"),
    }
    return company_ids, rows, context_notes, meta


def comparison_as_dicts(
    config_dir=None,
    company_ids: list[str] | None = None,
) -> dict[str, Any]:
    """JSON-friendly comparison payload for MCP / API reuse later."""
    company_order, rows, notes, meta = get_comparison_matrix(config_dir, company_ids)
    return {
        "companies": company_order,
        "last_reviewed": meta.get("last_reviewed"),
        "reviewed_by": meta.get("reviewed_by"),
        "capabilities": [
            {
                "id": row.capability_id,
                "label": row.capability_label,
                "claims": [
                    {
                        "company_id": c.company_id,
                        "company_label": c.company_label,
                        "claim": c.claim,
                        "source_url": c.source_url,
                        "quote": c.quote,
                        "is_unknown": c.is_unknown,
                    }
                    for c in row.claims
                ],
            }
            for row in rows
        ],
        "context_notes": notes,
    }
