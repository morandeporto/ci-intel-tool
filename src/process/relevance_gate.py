"""Per-source relevance gate before LLM classification.

gate: "off"  - official_competitor / emerging: never keyword-filtered.
gate: "strict" - industry / community: require ≥1 strong whole-word keyword.
exclude_title_patterns apply to ALL sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from src.config_loader import load_relevance_config
from src.ingest.normalize import NormalizedEntry


@dataclass(frozen=True)
class GateDecision:
    """Outcome of the relevance gate for one entry."""

    passed: bool
    reason: str | None = None
    strong_match_count: int = 0


def _compile_whole_word_pattern(keyword: str) -> re.Pattern[str]:
    """Match keyword as whole words, allow non-alnum inside phrases (e.g. CI/CD)."""
    parts = [re.escape(p) for p in keyword.split() if p]
    if not parts:
        return re.compile(r"(?!)")  # never matches
    # Word boundaries around the full phrase, interior spaces flexible.
    body = r"\s+".join(parts)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _count_keyword_matches(text: str, keywords: Sequence[str]) -> int:
    count = 0
    for kw in keywords:
        pattern = _compile_whole_word_pattern(kw)
        if pattern.search(text):
            count += 1
    return count


def _title_excluded(title: str, patterns: Sequence[str]) -> str | None:
    for pattern in patterns:
        if not pattern:
            continue
        if re.search(re.escape(pattern), title, flags=re.IGNORECASE):
            return f"exclude_title_pattern:{pattern}"
    return None


def evaluate_gate(
    entry: NormalizedEntry,
    *,
    gate: str,
    relevance_cfg: dict[str, Any] | None = None,
) -> GateDecision:
    """Decide whether an entry may proceed toward LLM selection.

    Filtered items should be stored with status=filtered by the caller.
    """
    cfg = relevance_cfg if relevance_cfg is not None else load_relevance_config()
    strong = list(cfg.get("strong_keywords") or [])
    exclude_patterns = list(cfg.get("exclude_title_patterns") or [])

    excluded = _title_excluded(entry.title or "", exclude_patterns)
    if excluded:
        return GateDecision(passed=False, reason=excluded, strong_match_count=0)

    text = f"{entry.title or ''} {entry.raw_excerpt or ''}"
    strong_count = _count_keyword_matches(text, strong)

    gate_mode = "off" if gate is False else str(gate)
    if gate_mode == "off":
        return GateDecision(passed=True, reason=None, strong_match_count=strong_count)

    if gate_mode != "strict":
        return GateDecision(
            passed=False,
            reason=f"invalid_gate:{gate_mode}",
            strong_match_count=strong_count,
        )

    if strong_count >= 1:
        return GateDecision(passed=True, reason=None, strong_match_count=strong_count)

    return GateDecision(
        passed=False,
        reason="no_strong_keyword",
        strong_match_count=0,
    )


def filter_by_gate(
    entries: Sequence[NormalizedEntry],
    *,
    gate: str,
    relevance_cfg: dict[str, Any] | None = None,
) -> tuple[list[NormalizedEntry], list[tuple[NormalizedEntry, GateDecision]]]:
    """Split entries into (passed, filtered_with_decision)."""
    cfg = relevance_cfg if relevance_cfg is not None else load_relevance_config()
    passed: list[NormalizedEntry] = []
    filtered: list[tuple[NormalizedEntry, GateDecision]] = []
    for entry in entries:
        decision = evaluate_gate(entry, gate=gate, relevance_cfg=cfg)
        if decision.passed:
            passed.append(entry)
        else:
            filtered.append((entry, decision))
    return passed, filtered
