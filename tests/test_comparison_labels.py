"""Tests for comparison matrix label formatting."""

from __future__ import annotations

from src.services.comparison import format_capability_label, get_comparison_matrix


def test_format_capability_label_fixes_aspom_and_keeps_acronyms():
    assert format_capability_label("Security Scanning / Aspom / Curation") == (
        "Security Scanning / ASPM / Curation"
    )
    assert format_capability_label("aspm") == "ASPM"
    assert "SCA" in format_capability_label("Software Composition / sca")
    assert "CI/CD" in format_capability_label("CI/CD integrations")
    assert "ML" in format_capability_label("ML / AI Artifact Management")
    assert "AI" in format_capability_label("ML / AI Artifact Management")


def test_security_scanning_row_shows_aspm_not_aspom():
    _companies, rows, _notes, _meta = get_comparison_matrix()
    security = next(r for r in rows if r.capability_id == "security_scanning")
    assert security.capability_label == "Security Scanning / ASPM / Curation"
    assert "Aspom" not in security.capability_label
