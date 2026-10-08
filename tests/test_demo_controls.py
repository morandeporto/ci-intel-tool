"""Dashboard demo notice and pipeline buttons (AppTest, GitHub calls mocked)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from src.db.connection import init_db
from src.services.github_dispatch import (
    LAST_DISPATCH_KEY,
    MISSING_TOKEN_MESSAGE,
    TOKEN_NAME,
    DispatchResult,
    WorkflowRun,
)
from src.ui import demo_controls

RUN_URL = "https://github.com/morandeporto/ci-intel-tool/actions/runs/42"


@pytest.fixture()
def local_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    db_path = tmp_path / "demo.db"
    init_db(db_path)
    monkeypatch.setenv("CI_INTEL_DB", str(db_path))
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    demo_controls._latest_runs.clear()
    return AppTest.from_file("src/ui/app.py", default_timeout=30)


def _markdown_text(app: AppTest) -> str:
    return " ".join(str(el.value) for el in app.markdown)


def _button(app: AppTest, key: str) -> Any:
    return next(b for b in app.button if b.key == f"demo_dispatch_{key}")


def test_notice_and_dedup_line_render(
    local_app: AppTest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_NAME, raising=False)
    local_app.run()

    assert not local_app.exception
    text = _markdown_text(local_app)
    for line in demo_controls.NOTICE_LINES:
        assert line in text
    assert "https://github.com/morandeporto/ci-intel-tool/actions" in text
    assert demo_controls.DEDUP_NOTE in text


def test_missing_token_disables_buttons_with_friendly_message(
    local_app: AppTest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOKEN_NAME, raising=False)
    local_app.run()

    assert not local_app.exception
    assert _button(local_app, "ingest").disabled
    assert _button(local_app, "rescore").disabled
    assert MISSING_TOKEN_MESSAGE in " ".join(str(c.value) for c in local_app.caption)


def test_click_dispatches_shows_run_link_and_locks_both_buttons(
    local_app: AppTest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_NAME, "ghp-test-token-not-real")
    dispatched: list[str] = []

    def fake_trigger(key: str, state: Any, **_kwargs: Any) -> DispatchResult:
        dispatched.append(key)
        state[LAST_DISPATCH_KEY] = 10**12  # far future → cooldown active
        return DispatchResult(ok=True, message="The run was started.", run_url=RUN_URL)

    monkeypatch.setattr(demo_controls, "trigger_workflow", fake_trigger)
    monkeypatch.setattr(
        demo_controls,
        "latest_workflow_run",
        lambda key, **_k: WorkflowRun("queued", None, RUN_URL, "2026-10-08T09:00:00Z"),
    )

    local_app.run()
    assert not _button(local_app, "ingest").disabled
    _button(local_app, "ingest").click().run()

    assert not local_app.exception
    assert dispatched == ["ingest"]
    success = " ".join(str(s.value) for s in local_app.success)
    assert RUN_URL in success
    assert demo_controls.AFTER_CLICK_NOTE in success
    assert _button(local_app, "ingest").disabled
    assert _button(local_app, "rescore").disabled
    assert "queued" in _markdown_text(local_app)


def test_api_error_shows_message_and_rest_of_app_renders(
    local_app: AppTest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_NAME, "ghp-test-token-not-real")

    def failing_status(key: str, **_kwargs: Any) -> WorkflowRun:
        raise demo_controls.GitHubApiError("Could not reach GitHub right now.")

    monkeypatch.setattr(demo_controls, "latest_workflow_run", failing_status)
    monkeypatch.setattr(
        demo_controls,
        "trigger_workflow",
        lambda key, state, **_k: DispatchResult(ok=False, message="GitHub said no."),
    )

    local_app.run()
    _button(local_app, "rescore").click().run()

    assert not local_app.exception
    assert "GitHub said no." in " ".join(str(i.value) for i in local_app.info)
    assert "unavailable" in " ".join(str(c.value) for c in local_app.caption)
    assert not _button(local_app, "rescore").disabled, "failed dispatch stays retryable"
    assert "No items yet" in " ".join(str(i.value) for i in local_app.info)
