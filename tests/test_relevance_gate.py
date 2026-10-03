"""Tests for per-source relevance gate (off vs strict)."""

from __future__ import annotations

from src.ingest.normalize import NormalizedEntry
from src.process.relevance_gate import evaluate_gate, filter_by_gate


def _entry(title: str, excerpt: str = "") -> NormalizedEntry:
    return NormalizedEntry(
        title=title,
        url="https://example.com/" + title[:20].replace(" ", "-"),
        published_at="2026-10-03T10:00:00+00:00",
        raw_excerpt=excerpt,
        source_id="test",
        competitor="industry",
        content_hash="h-" + title,
    )


RELEVANCE = {
    "strong_keywords": [
        "supply chain",
        "SBOM",
        "CVE",
        "Snyk",
        "JFrog",
        "npm",
    ],
    "exclude_title_patterns": [
        "Planned Cloud Maintenance",
        "Scheduled Maintenance",
    ],
}


def test_gate_off_passes_without_keywords() -> None:
    entry = _entry("Our quarterly roadmap update")
    decision = evaluate_gate(entry, gate="off", relevance_cfg=RELEVANCE)
    assert decision.passed is True
    assert decision.reason is None


def test_gate_off_still_applies_exclude_title_patterns() -> None:
    entry = _entry("Planned Cloud Maintenance for EU region")
    decision = evaluate_gate(entry, gate="off", relevance_cfg=RELEVANCE)
    assert decision.passed is False
    assert decision.reason and decision.reason.startswith("exclude_title_pattern:")


def test_strict_requires_strong_keyword() -> None:
    entry = _entry("New SBOM guidance for vendors")
    decision = evaluate_gate(entry, gate="strict", relevance_cfg=RELEVANCE)
    assert decision.passed is True
    assert decision.strong_match_count >= 1


def test_strict_non_strong_terms_alone_never_pass() -> None:
    entry = _entry("Docker container security and vulnerability scanning")
    decision = evaluate_gate(entry, gate="strict", relevance_cfg=RELEVANCE)
    assert decision.passed is False
    assert decision.reason == "no_strong_keyword"


def test_strict_strong_in_summary_passes() -> None:
    entry = _entry("Weekly digest", excerpt="A major npm supply chain incident was disclosed.")
    decision = evaluate_gate(entry, gate="strict", relevance_cfg=RELEVANCE)
    assert decision.passed is True


def test_whole_word_match_avoids_substring_false_positive() -> None:
    # "npm" must not match inside "companypackage" style tokens, "Snyk" not in "Snyker".
    entry = _entry("Meet the Snyker team at the conference")
    decision = evaluate_gate(entry, gate="strict", relevance_cfg=RELEVANCE)
    assert decision.passed is False


def test_scheduled_maintenance_excluded_for_all_gates() -> None:
    entry = _entry("Scheduled Maintenance window")
    for gate in ("off", "strict"):
        decision = evaluate_gate(entry, gate=gate, relevance_cfg=RELEVANCE)
        assert decision.passed is False


def test_filter_by_gate_splits_passed_and_filtered() -> None:
    entries = [
        _entry("SBOM mandate update"),
        _entry("Random container news"),
    ]
    passed, filtered = filter_by_gate(entries, gate="strict", relevance_cfg=RELEVANCE)
    assert len(passed) == 1
    assert passed[0].title.startswith("SBOM")
    assert len(filtered) == 1
    assert filtered[0][1].reason == "no_strong_keyword"
