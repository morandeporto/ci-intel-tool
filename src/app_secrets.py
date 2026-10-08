"""Secret lookup: environment variables first, then Streamlit ``st.secrets``.

Environment variables cover local ``.env`` runs and GitHub Actions. Streamlit
Community Cloud injects its secrets through ``st.secrets`` instead. Values are
never logged or rendered, callers only learn whether a secret is set.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Copied from .env.example, treated as unset so local demos stay on SQLite / no key.
PLACEHOLDER_MARKERS = ("your-",)


def _from_streamlit_secrets(name: str) -> str:
    """Read ``name`` from st.secrets, or "" when Streamlit or secrets.toml is absent."""
    try:
        import streamlit as st

        # load_if_toml_exists() suppresses the on-page st.error a missing file triggers.
        if not st.secrets.load_if_toml_exists():
            return ""
        value = st.secrets.get(name)
    except Exception as exc:  # noqa: BLE001 - a broken secrets file must not crash callers
        logger.warning("Could not read Streamlit secrets (%s)", type(exc).__name__)
        return ""
    if value is None or not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def get_secret(name: str) -> str:
    """Return the secret ``name`` (stripped) or "" when unset or a placeholder."""
    value = os.environ.get(name, "").strip()
    if not value:
        value = _from_streamlit_secrets(name)
    if not value or is_placeholder(value):
        return ""
    return value
