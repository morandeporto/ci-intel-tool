"""get_secret: environment first, then st.secrets (Streamlit Community Cloud)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from src.app_secrets import get_secret
from src.db.connection import turso_configured

pytestmark = pytest.mark.real_streamlit_secrets


class _FakeSecrets(dict):
    def __init__(self, values: dict[str, Any], *, file_exists: bool = True) -> None:
        super().__init__(values)
        self._file_exists = file_exists

    def load_if_toml_exists(self) -> bool:
        return self._file_exists


def _install_fake_streamlit(
    monkeypatch: pytest.MonkeyPatch, secrets: Any
) -> None:
    fake = types.ModuleType("streamlit")
    fake.secrets = secrets  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "streamlit", fake)


def test_env_var_wins_over_streamlit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "env-value")
    _install_fake_streamlit(monkeypatch, _FakeSecrets({"GEMINI_API_KEY": "secrets-value"}))
    assert get_secret("GEMINI_API_KEY") == "env-value"


def test_falls_back_to_streamlit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    _install_fake_streamlit(
        monkeypatch, _FakeSecrets({"GITHUB_DISPATCH_TOKEN": "  ghp-from-secrets  "})
    )
    assert get_secret("GITHUB_DISPATCH_TOKEN") == "ghp-from-secrets"


def test_turso_configured_from_streamlit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    _install_fake_streamlit(
        monkeypatch,
        _FakeSecrets(
            {
                "TURSO_DATABASE_URL": "libsql://demo-db.turso.io",
                "TURSO_AUTH_TOKEN": "token-value",
            }
        ),
    )
    assert turso_configured() is True


def test_missing_secrets_file_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _install_fake_streamlit(
        monkeypatch, _FakeSecrets({"GEMINI_API_KEY": "unused"}, file_exists=False)
    )
    assert get_secret("GEMINI_API_KEY") == ""


def test_missing_key_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    _install_fake_streamlit(monkeypatch, _FakeSecrets({}))
    assert get_secret("GITHUB_DISPATCH_TOKEN") == ""


def test_placeholder_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "your-gemini-api-key-here")
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    _install_fake_streamlit(
        monkeypatch,
        _FakeSecrets({"TURSO_DATABASE_URL": "libsql://your-db-name-org.turso.io"}),
    )
    assert get_secret("GEMINI_API_KEY") == ""
    assert get_secret("TURSO_DATABASE_URL") == ""


def test_broken_secrets_object_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        def load_if_toml_exists(self) -> bool:
            raise ValueError("malformed secrets.toml")

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _install_fake_streamlit(monkeypatch, _Broken())
    assert get_secret("GEMINI_API_KEY") == ""
