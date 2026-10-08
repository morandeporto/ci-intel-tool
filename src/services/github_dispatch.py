"""Trigger the existing GitHub Actions workflows from the dashboard (workflow_dispatch).

The dashboard never runs ingestion: it asks GitHub to start daily_ingest.yml or
retry_pending.yml on ``main``. The token (GITHUB_DISPATCH_TOKEN) only travels in
the Authorization header. It is never logged, rendered, or put in an error message.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.app_secrets import get_secret
from src.config_loader import load_demo_config

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TOKEN_NAME = "GITHUB_DISPATCH_TOKEN"
LAST_DISPATCH_KEY = "_ci_demo_last_dispatch_at"
# GitHub and app clocks can differ a little, accept runs created slightly "before" the click.
RUN_LOOKUP_CLOCK_SLACK = timedelta(seconds=10)

MISSING_TOKEN_MESSAGE = (
    "The pipeline buttons are not configured on this deployment. "
    "The scheduled daily runs still happen, see GitHub Actions."
)
UNREACHABLE_MESSAGE = (
    "Could not reach GitHub right now. Please try again in a minute, "
    "or open GitHub Actions directly."
)


@dataclass(frozen=True)
class DemoSettings:
    repo: str
    ref: str
    workflows: dict[str, str]
    cooldown_seconds: float
    timeout_seconds: float
    run_lookup_attempts: int
    run_lookup_delay_seconds: float

    @property
    def actions_url(self) -> str:
        return f"https://github.com/{self.repo}/actions"

    def workflow_page_url(self, workflow_key: str) -> str:
        return f"{self.actions_url}/workflows/{self.workflows[workflow_key]}"


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    message: str
    run_url: str | None = None


@dataclass(frozen=True)
class WorkflowRun:
    status: str
    conclusion: str | None
    html_url: str
    created_at: str

    @property
    def label(self) -> str:
        """Plain status text: queued / in progress / success / failure ..."""
        if self.status == "completed":
            return (self.conclusion or "completed").replace("_", " ")
        return self.status.replace("_", " ")


class GitHubApiError(Exception):
    """Non-sensitive, user-facing failure talking to the GitHub REST API."""


def load_demo_settings() -> DemoSettings:
    data = load_demo_config()
    return DemoSettings(
        repo=str(data["github_repo"]).strip(),
        ref=str(data.get("ref") or "main").strip(),
        workflows={str(k): str(v) for k, v in data["workflows"].items()},
        cooldown_seconds=float(data.get("cooldown_seconds", 180)),
        timeout_seconds=float(data.get("request_timeout_seconds", 10)),
        run_lookup_attempts=max(0, int(data.get("run_lookup_attempts", 3))),
        run_lookup_delay_seconds=max(0.0, float(data.get("run_lookup_delay_seconds", 2.0))),
    )


def dispatch_token() -> str:
    return get_secret(TOKEN_NAME)


def cooldown_remaining(
    state: MutableMapping[str, Any], *, cooldown_seconds: float, now: float
) -> float:
    """Seconds until the buttons unlock again for this session (0 when unlocked)."""
    last = state.get(LAST_DISPATCH_KEY)
    if last is None:
        return 0.0
    return max(0.0, float(last) + cooldown_seconds - now)


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ci-intel-tool-demo",
    }


def _error_for_status(status_code: int) -> GitHubApiError:
    if status_code in (401, 403):
        text = "GitHub rejected the request (the demo token may be expired or lack access)."
    elif status_code == 404:
        text = "GitHub could not find the workflow for this demo."
    elif status_code == 422:
        text = "GitHub refused to start the workflow on this branch."
    elif status_code == 429:
        text = "GitHub is rate limiting requests. Please try again in a minute."
    else:
        text = f"GitHub returned an unexpected response (HTTP {status_code})."
    return GitHubApiError(text)


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    try:
        response = client.request(
            method,
            f"{GITHUB_API}{path}",
            headers=_headers(token),
            json=json_body,
            params=params,
        )
    except httpx.HTTPError as exc:
        logger.warning("GitHub API %s %s failed: %s", method, path, type(exc).__name__)
        raise GitHubApiError(UNREACHABLE_MESSAGE) from exc
    if response.status_code >= 400:
        logger.warning("GitHub API %s %s returned HTTP %s", method, path, response.status_code)
        raise _error_for_status(response.status_code)
    return response


def _parse_runs(payload: Any) -> list[WorkflowRun]:
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise GitHubApiError("GitHub returned an unexpected response.")
    parsed: list[WorkflowRun] = []
    for run in runs:
        if not isinstance(run, dict) or not run.get("html_url"):
            continue
        parsed.append(
            WorkflowRun(
                status=str(run.get("status") or "unknown"),
                conclusion=(str(run["conclusion"]) if run.get("conclusion") else None),
                html_url=str(run["html_url"]),
                created_at=str(run.get("created_at") or ""),
            )
        )
    return parsed


def _parse_github_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _list_runs(
    client: httpx.Client,
    settings: DemoSettings,
    workflow_key: str,
    *,
    token: str,
    per_page: int,
    event: str | None = None,
) -> list[WorkflowRun]:
    params: dict[str, Any] = {"branch": settings.ref, "per_page": per_page}
    if event:
        params["event"] = event
    response = _request(
        client,
        "GET",
        f"/repos/{settings.repo}/actions/workflows/{settings.workflows[workflow_key]}/runs",
        token=token,
        params=params,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubApiError("GitHub returned an unexpected response.") from exc
    return _parse_runs(payload)


def _find_new_run_url(
    client: httpx.Client,
    settings: DemoSettings,
    workflow_key: str,
    *,
    token: str,
    dispatched_at: datetime,
    sleep: Callable[[float], None],
) -> str | None:
    """The dispatch API returns no run id, so look for a run created after the click."""
    for attempt in range(settings.run_lookup_attempts):
        if attempt:
            sleep(settings.run_lookup_delay_seconds)
        try:
            runs = _list_runs(
                client, settings, workflow_key, token=token, per_page=5, event="workflow_dispatch"
            )
        except GitHubApiError:
            return None
        for run in runs:
            created = _parse_github_time(run.created_at)
            if created is not None and created >= dispatched_at - RUN_LOOKUP_CLOCK_SLACK:
                return run.html_url
    return None


def trigger_workflow(
    workflow_key: str,
    state: MutableMapping[str, Any],
    *,
    settings: DemoSettings,
    token: str | None = None,
    client: httpx.Client | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> DispatchResult:
    """Dispatch ``workflow_key`` on the configured ref, honouring the session cooldown.

    ``state`` is the Streamlit session_state (any mutable mapping in tests). The
    cooldown starts only after GitHub accepted the dispatch, so a failed call can
    be retried right away.
    """
    if workflow_key not in settings.workflows:
        return DispatchResult(ok=False, message="Unknown pipeline action.")
    wait = cooldown_remaining(state, cooldown_seconds=settings.cooldown_seconds, now=now())
    if wait > 0:
        return DispatchResult(
            ok=False,
            message=f"A run was just started. The buttons unlock in {int(wait) + 1} seconds.",
        )
    token = dispatch_token() if token is None else token
    if not token:
        return DispatchResult(ok=False, message=MISSING_TOKEN_MESSAGE)

    owns_client = client is None
    client = client or httpx.Client(timeout=settings.timeout_seconds)
    try:
        dispatched_at = datetime.now(timezone.utc)
        _request(
            client,
            "POST",
            f"/repos/{settings.repo}/actions/workflows/"
            f"{settings.workflows[workflow_key]}/dispatches",
            token=token,
            json_body={"ref": settings.ref},
        )
        state[LAST_DISPATCH_KEY] = now()
        run_url = _find_new_run_url(
            client,
            settings,
            workflow_key,
            token=token,
            dispatched_at=dispatched_at,
            sleep=sleep,
        )
    except GitHubApiError as exc:
        return DispatchResult(ok=False, message=str(exc))
    finally:
        if owns_client:
            client.close()
    return DispatchResult(
        ok=True,
        message="The run was started on GitHub Actions.",
        run_url=run_url or settings.workflow_page_url(workflow_key),
    )


def latest_workflow_run(
    workflow_key: str,
    *,
    settings: DemoSettings,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> WorkflowRun | None:
    """Most recent run of ``workflow_key`` on the configured ref (any trigger)."""
    token = dispatch_token() if token is None else token
    if not token:
        raise GitHubApiError(MISSING_TOKEN_MESSAGE)
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.timeout_seconds)
    try:
        runs = _list_runs(client, settings, workflow_key, token=token, per_page=1)
    finally:
        if owns_client:
            client.close()
    return runs[0] if runs else None
