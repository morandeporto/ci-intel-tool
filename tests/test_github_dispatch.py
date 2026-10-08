"""Dashboard → GitHub workflow_dispatch (mocked transport, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pytest

from src.services.github_dispatch import (
    LAST_DISPATCH_KEY,
    MISSING_TOKEN_MESSAGE,
    TOKEN_NAME,
    DemoSettings,
    GitHubApiError,
    cooldown_remaining,
    latest_workflow_run,
    load_demo_settings,
    trigger_workflow,
)

TOKEN = "ghp-test-token-not-real"
SETTINGS = DemoSettings(
    repo="owner/ci-intel-tool",
    ref="main",
    workflows={"ingest": "daily_ingest.yml", "rescore": "retry_pending.yml"},
    cooldown_seconds=180,
    timeout_seconds=5,
    run_lookup_attempts=2,
    run_lookup_delay_seconds=0,
)
RUN_URL = "https://github.com/owner/ci-intel-tool/actions/runs/123"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _runs_payload(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(runs), "workflow_runs": list(runs)}


def _recording_handler(
    requests: list[httpx.Request],
    *,
    dispatch_status: int = 204,
    runs: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(dispatch_status, json={"message": "nope"})
        return httpx.Response(200, json=_runs_payload(*(runs or [])))

    return handler


def test_dispatch_success_posts_ref_and_returns_new_run_link() -> None:
    requests: list[httpx.Request] = []
    new_run = {
        "status": "queued",
        "conclusion": None,
        "html_url": RUN_URL,
        "created_at": _iso(datetime.now(timezone.utc)),
    }
    state: dict[str, Any] = {}

    result = trigger_workflow(
        "ingest",
        state,
        settings=SETTINGS,
        token=TOKEN,
        client=_client(_recording_handler(requests, runs=[new_run])),
        now=lambda: 1000.0,
    )

    assert result.ok is True
    assert result.run_url == RUN_URL
    post = requests[0]
    assert post.method == "POST"
    assert post.url.path == (
        "/repos/owner/ci-intel-tool/actions/workflows/daily_ingest.yml/dispatches"
    )
    assert json.loads(post.content) == {"ref": "main"}
    assert post.headers["Authorization"] == f"Bearer {TOKEN}"
    lookup = requests[1]
    assert lookup.url.params["event"] == "workflow_dispatch"
    assert lookup.url.params["branch"] == "main"
    assert state[LAST_DISPATCH_KEY] == 1000.0


def test_dispatch_falls_back_to_workflow_page_when_run_not_visible_yet() -> None:
    requests: list[httpx.Request] = []
    old_run = {
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/owner/ci-intel-tool/actions/runs/1",
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(hours=5)),
    }

    result = trigger_workflow(
        "rescore",
        {},
        settings=SETTINGS,
        token=TOKEN,
        client=_client(_recording_handler(requests, runs=[old_run])),
    )

    assert result.ok is True
    assert result.run_url == (
        "https://github.com/owner/ci-intel-tool/actions/workflows/retry_pending.yml"
    )
    assert len(requests) == 1 + SETTINGS.run_lookup_attempts


def test_missing_token_makes_no_request_and_no_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_NAME, raising=False)
    requests: list[httpx.Request] = []
    state: dict[str, Any] = {}

    result = trigger_workflow(
        "ingest", state, settings=SETTINGS, client=_client(_recording_handler(requests))
    )

    assert result.ok is False
    assert result.message == MISSING_TOKEN_MESSAGE
    assert requests == []
    assert LAST_DISPATCH_KEY not in state


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "expired"), (403, "expired"), (404, "could not find"), (422, "refused"), (500, "500")],
)
def test_api_error_is_friendly_and_never_leaks_token(status: int, needle: str) -> None:
    state: dict[str, Any] = {}

    result = trigger_workflow(
        "ingest",
        state,
        settings=SETTINGS,
        token=TOKEN,
        client=_client(_recording_handler([], dispatch_status=status)),
    )

    assert result.ok is False
    assert needle in result.message
    assert TOKEN not in result.message
    assert LAST_DISPATCH_KEY not in state, "a failed dispatch must stay retryable"


def test_network_error_is_friendly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    result = trigger_workflow(
        "ingest", {}, settings=SETTINGS, token=TOKEN, client=_client(handler)
    )

    assert result.ok is False
    assert "Could not reach GitHub" in result.message


def test_cooldown_blocks_both_buttons_then_expires() -> None:
    requests: list[httpx.Request] = []
    client = _client(_recording_handler(requests))
    state: dict[str, Any] = {}
    clock = {"t": 1000.0}

    first = trigger_workflow(
        "ingest", state, settings=SETTINGS, token=TOKEN, client=client, now=lambda: clock["t"]
    )
    assert first.ok is True
    posts_after_first = sum(r.method == "POST" for r in requests)

    clock["t"] = 1000.0 + 60
    assert cooldown_remaining(state, cooldown_seconds=180, now=clock["t"]) == 120
    blocked = trigger_workflow(
        "rescore", state, settings=SETTINGS, token=TOKEN, client=client, now=lambda: clock["t"]
    )
    assert blocked.ok is False
    assert "unlock in 121 seconds" in blocked.message
    assert sum(r.method == "POST" for r in requests) == posts_after_first

    clock["t"] = 1000.0 + 180
    assert cooldown_remaining(state, cooldown_seconds=180, now=clock["t"]) == 0
    again = trigger_workflow(
        "rescore", state, settings=SETTINGS, token=TOKEN, client=client, now=lambda: clock["t"]
    )
    assert again.ok is True


def test_latest_run_reports_status_label() -> None:
    run = {
        "status": "completed",
        "conclusion": "success",
        "html_url": RUN_URL,
        "created_at": "2026-10-08T04:17:00Z",
    }
    latest = latest_workflow_run(
        "ingest",
        settings=SETTINGS,
        token=TOKEN,
        client=_client(_recording_handler([], runs=[run])),
    )
    assert latest is not None
    assert latest.label == "success"
    assert latest.html_url == RUN_URL


def test_latest_run_without_token_raises_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_NAME, raising=False)
    with pytest.raises(GitHubApiError, match="not configured"):
        latest_workflow_run("ingest", settings=SETTINGS)


def test_shipped_demo_config_points_at_existing_workflows() -> None:
    from src.config_loader import PROJECT_ROOT

    settings = load_demo_settings()
    assert settings.ref == "main"
    assert settings.cooldown_seconds == 180
    for workflow_file in settings.workflows.values():
        assert (PROJECT_ROOT / ".github" / "workflows" / workflow_file).is_file()
