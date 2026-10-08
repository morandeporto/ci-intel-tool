"""Open review demo: notice banner and the two GitHub workflow buttons.

The buttons only send a workflow_dispatch (src/services/github_dispatch.py),
ingestion and scoring still run on GitHub Actions, never in the dashboard.
"""

from __future__ import annotations

import html
import time

import streamlit as st

from src.config_loader import ConfigError
from src.services.github_dispatch import (
    MISSING_TOKEN_MESSAGE,
    DemoSettings,
    GitHubApiError,
    cooldown_remaining,
    dispatch_token,
    latest_workflow_run,
    load_demo_settings,
    trigger_workflow,
)
from src.ui.components import format_israel_time

NOTICE_LINES = (
    "Live demo for the review. Intentionally open, on temporary free-tier accounts "
    "that will be deleted after the review.",
    "Pages can be slow: the app server and the free database are in different regions.",
)
BUTTONS = (("ingest", "Run daily ingest"), ("rescore", "Retry scoring"))
DEDUP_NOTE = (
    "If there is no new news since the last run, the run reports 0 new items. "
    "That is deduplication working."
)
AFTER_CLICK_NOTE = (
    "It takes 2-3 minutes; then refresh the page and check the Pipeline runs tab."
)
RESULT_KEY = "_ci_demo_dispatch_result"
AUTO_REFRESH_KEY = "_ci_demo_auto_refresh"
STATUS_CACHE_SECONDS = 20
COOLDOWN_REFRESH_SECONDS = 15


def _render_notice(actions_url: str | None) -> None:
    lines = "".join(f"<p>{html.escape(line)}</p>" for line in NOTICE_LINES)
    link = (
        f'<a href="{html.escape(actions_url)}" target="_blank" rel="noopener">'
        "Pipeline runs on GitHub Actions ›</a>"
        if actions_url
        else ""
    )
    st.markdown(f'<div class="ci-demo-notice">{lines}{link}</div>', unsafe_allow_html=True)


@st.cache_data(ttl=STATUS_CACHE_SECONDS, show_spinner=False)
def _latest_runs() -> dict[str, dict[str, str]]:
    """Latest run per button, or {"error": ...}. Cached so reruns do not hit the API."""
    settings = load_demo_settings()
    out: dict[str, dict[str, str]] = {}
    for key, _label in BUTTONS:
        try:
            run = latest_workflow_run(key, settings=settings)
        except GitHubApiError as exc:
            return {"_error": {"message": str(exc)}}
        if run is not None:
            out[key] = {
                "label": run.label,
                "url": run.html_url,
                "created_at": run.created_at,
            }
    return out


def _render_latest_runs() -> None:
    runs = _latest_runs()
    if "_error" in runs:
        st.caption(f"Latest run status is unavailable: {runs['_error']['message']}")
        return
    parts: list[str] = []
    for key, label in BUTTONS:
        run = runs.get(key)
        if run is None:
            parts.append(f"{html.escape(label)}: no runs yet")
            continue
        parts.append(
            f'{html.escape(label)}: <a href="{html.escape(run["url"])}" target="_blank" '
            f'rel="noopener">{html.escape(run["label"])} '
            f'({format_israel_time(run["created_at"])}) ›</a>'
        )
    st.markdown(
        '<div class="ci-demo-status">Latest runs · ' + " · ".join(parts) + "</div>",
        unsafe_allow_html=True,
    )


def _render_last_result() -> None:
    result = st.session_state.get(RESULT_KEY)
    if not result:
        return
    if result["ok"]:
        st.success(
            f"{result['label']}: {result['message']} "
            f"[Open the run on GitHub Actions ›]({result['run_url']})\n\n{AFTER_CLICK_NOTE}"
        )
    else:
        st.info(f"{result['label']}: {result['message']}")


def _on_click(settings: DemoSettings, key: str, label: str) -> None:
    with st.spinner("Asking GitHub Actions to start the run…"):
        result = trigger_workflow(key, st.session_state, settings=settings)
    st.session_state[RESULT_KEY] = {
        "ok": result.ok,
        "label": label,
        "message": result.message,
        "run_url": result.run_url,
    }
    if result.ok:
        _latest_runs.clear()
        st.session_state[AUTO_REFRESH_KEY] = True
        # Full rerun so the fragment is rebuilt with auto-refresh during the cooldown.
        st.rerun()


def _controls_body(settings: DemoSettings, has_token: bool) -> None:
    wait = cooldown_remaining(
        st.session_state, cooldown_seconds=settings.cooldown_seconds, now=time.time()
    )
    if wait <= 0 and st.session_state.pop(AUTO_REFRESH_KEY, False):
        # Cooldown just ended: full rerun drops the fragment's auto-refresh timer.
        st.rerun()

    cols = st.columns([1, 1, 3])
    clicked: tuple[str, str] | None = None
    for col, (key, label) in zip(cols[:2], BUTTONS):
        with col:
            if st.button(
                label,
                key=f"demo_dispatch_{key}",
                type="primary" if key == "ingest" else "secondary",
                disabled=(wait > 0 or not has_token),
                use_container_width=True,
            ):
                clicked = (key, label)
    with cols[2]:
        st.markdown(
            f'<div class="ci-demo-hint">{html.escape(DEDUP_NOTE)}</div>',
            unsafe_allow_html=True,
        )

    if clicked is not None:
        _on_click(settings, *clicked)

    if not has_token:
        st.caption(MISSING_TOKEN_MESSAGE)
        return
    if wait > 0:
        minutes, seconds = divmod(int(wait) + 1, 60)
        st.caption(f"Buttons unlock in {minutes}:{seconds:02d}.")
    _render_last_result()
    _render_latest_runs()


def render_demo_section() -> None:
    """Demo notice, then two buttons that dispatch the GitHub workflows."""
    try:
        settings = load_demo_settings()
    except (ConfigError, KeyError, ValueError):
        _render_notice(None)
        st.caption("Pipeline buttons are unavailable (config/demo.yaml is invalid).")
        return
    _render_notice(settings.actions_url)
    has_token = bool(dispatch_token())
    wait = cooldown_remaining(
        st.session_state, cooldown_seconds=settings.cooldown_seconds, now=time.time()
    )
    run_every = COOLDOWN_REFRESH_SECONDS if wait > 0 else None
    st.fragment(_controls_body, run_every=run_every)(settings, has_token)
