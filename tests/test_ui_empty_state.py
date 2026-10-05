"""Dashboard empty state must not trigger ingestion."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import init_db
from src.ui import pending_actions


def test_run_now_is_not_a_pending_action() -> None:
    assert "run_now" not in pending_actions.ACTION_LABELS


def test_empty_digest_renders_without_pipeline_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "empty.db"
    init_db(db_path)
    monkeypatch.setenv("CI_INTEL_DB", str(db_path))
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)

    calls: list[object] = []

    def _forbid_pipeline(*_args: object, **_kwargs: object) -> None:
        calls.append((_args, _kwargs))
        raise AssertionError("UI must not call run_daily")

    monkeypatch.setattr("src.pipeline.run_daily.run_daily", _forbid_pipeline)

    app = AppTest.from_file("src/ui/app.py", default_timeout=30)
    app.run()

    assert not app.exception
    info_text = " ".join(str(el.value) for el in app.info)
    assert "No items yet" in info_text
    assert "Pipeline runs" in info_text
    assert "Run Now" not in info_text
    assert all(btn.label != "Run Now" for btn in app.button)
    assert calls == []
