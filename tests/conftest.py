"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_streamlit_secrets: run the real st.secrets fallback (streamlit is mocked).",
    )


@pytest.fixture(autouse=True)
def _no_streamlit_secrets(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep tests off a developer's real .streamlit/secrets.toml (Turso, Gemini).

    Tests that clear env vars expect local SQLite and no key. Without this guard
    the st.secrets fallback could silently connect them to the shared database.
    """
    if request.node.get_closest_marker("real_streamlit_secrets"):
        return
    monkeypatch.setattr("src.app_secrets._from_streamlit_secrets", lambda name: "")
