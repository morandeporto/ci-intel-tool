"""Streamlit entrypoint for the CI Intel Tool.

Run from repo root:
  .venv/bin/streamlit run src/ui/app.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import is_connection_error, turso_configured
from src.db.models import DIMENSION_NAMES
from src.db.repository import Repository
from src.services.comparison import get_comparison_matrix
from src.services.digest import (
    digest_kpis,
    filter_digest_by_news_dates,
    israel_today,
    item_news_date,
    list_digest,
)
from src.services.feedback import get_latest_feedback
from src.services.weights import get_effective_weights
from src.ui.components import (
    competitor_labels,
    load_styles,
    render_banner,
    render_comparison_matrix,
    render_kpi,
    render_news_card,
    render_run_history,
)
from src.ui.db_session import get_repository, mark_db_dirty
from src.ui.host_chrome import boot_host_chrome
from src.ui.pending_actions import (
    flash,
    handle_pending_action,
    pop_flash,
    queue_action,
)

CSS_PATH = Path(__file__).resolve().parent / "styles.css"
DIM_LABELS = {
    "jfrog_relevance": "JFrog relevance",
    "competitor_signal": "Market and competitive pressure",
    "strategic_impact": "Strategic impact",
    "freshness": "Freshness",
    "market_visibility": "Market visibility",
}
WEIGHT_SUM_TOLERANCE = 0.01


def _inject_css() -> None:
    st.markdown(f"<style>{load_styles(CSS_PATH)}</style>", unsafe_allow_html=True)


def _weight_sum_status(total: float) -> tuple[str, str]:
    if total > 1.0 + WEIGHT_SUM_TOLERANCE:
        return ("error", f"Sum {total:.2f} — over 1.00. Lower a slider.")
    if abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE:
        return ("ok", f"Sum {total:.2f} / 1.00 — ready to save.")
    return ("warn", f"Sum {total:.2f} / 1.00 — need {1.0 - total:.2f} more.")


def _try_run_pipeline(db_path: Path | None) -> tuple[bool, str]:
    try:
        from src.config_loader import DEFAULT_DB_PATH
        from src.pipeline.run_daily import run_daily
    except ImportError:
        return False, "Pipeline module is not available."
    try:
        target = None if turso_configured() else DEFAULT_DB_PATH
        result = run_daily(trigger="manual", db_path=target, limit=10)
        mark_db_dirty()
        parts = [
            f"status={result.status}",
            f"fetched={result.items_fetched}",
            f"new={result.items_new}",
            f"scored={result.items_scored}",
        ]
        if result.message:
            parts.append(result.message)
        msg = "Pipeline " + ", ".join(parts)
        if result.status == "failed" or (
            result.items_scored == 0 and result.items_new > 0
        ):
            return False, msg
        if result.items_scored == 0 and result.items_new == 0:
            return True, msg + " (nothing new to add)"
        return True, msg
    except Exception as exc:  # noqa: BLE001
        return False, f"Pipeline failed: {exc}"


@st.fragment
def _weight_editor_fragment(repo: Repository) -> None:
    """Desktop: sliders row + side panel. Mobile: columns stack naturally. No frames."""
    current = st.session_state.applied_weights

    st.markdown('<div class="ci-section-title">Weight tuning</div>', unsafe_allow_html=True)
    st.caption("Edit sliders to sum **1.00**, then Save. Ranking updates only after save.")

    left, right = st.columns([3.2, 1.15], gap="medium")

    with left:
        cols = st.columns(len(DIMENSION_NAMES))
        raw_weights: dict[str, float] = {}
        for col, name in zip(cols, DIMENSION_NAMES):
            with col:
                raw_weights[name] = st.slider(
                    DIM_LABELS.get(name, name),
                    min_value=0.0,
                    max_value=1.0,
                    value=float(current.get(name, 0.2)),
                    step=0.01,
                    key=f"draft_w_{name}",
                )

    total = float(sum(raw_weights.values()))
    dirty = any(
        abs(float(raw_weights[k]) - float(current.get(k, 0.0))) > 1e-9
        for k in DIMENSION_NAMES
    )
    level, message = _weight_sum_status(total)
    bar_color = {"ok": "#40BE46", "warn": "#E6A23C", "error": "#E74C3C"}[level]
    fill = min(max(total, 0.0), 1.0) * 100
    can_save = dirty and abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE

    with right:
        if dirty:
            hint = message
            hint_cls = f"ci-weight-hint ci-weight-hint-{level}"
        else:
            hint = "Move a slider to enable Save."
            hint_cls = "ci-weight-hint"
        st.markdown(
            f"""
            <div class="ci-weight-panel">
              <div class="ci-weight-sum-row">
                <span>Weight sum</span><span>{total:.2f} / 1.00</span>
              </div>
              <div class="ci-weight-bar">
                <div class="ci-weight-bar-fill" style="width:{fill:.1f}%;background:{bar_color};"></div>
              </div>
              <p class="{hint_cls}">{hint}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if dirty:
            if st.button(
                "Save weights",
                type="primary",
                disabled=not can_save,
                use_container_width=True,
                key="save_weights_btn",
            ):
                queue_action(
                    "save_weights",
                    {"weights": {k: float(v) for k, v in raw_weights.items()}},
                )


def _render_kpis(kpis: dict) -> None:
    avg = f"{kpis['avg_score']:.2f}" if kpis["avg_score"] is not None else "—"
    cards = [
        (str(kpis["item_count"]), "News items", False),
        (avg, "Avg relevance", True),
        (str(kpis["high_score_count"]), "High scores (≥4)", False),
        (str(kpis["feedback_count"]), "Feedback signals", False),
    ]
    cols = st.columns(4)
    for col, (value, label, glow) in zip(cols, cards):
        with col:
            st.markdown(render_kpi(value, label, glow=glow), unsafe_allow_html=True)


def _render_feedback_row(repo: Repository, item: dict) -> None:
    item_id = str(item["id"])
    pending_key = "pending_down_id"
    is_editing_down = st.session_state.get(pending_key) == item_id
    latest = get_latest_feedback(repo, item_id)
    saved_vote = (latest or {}).get("vote")
    saved_rationale = ((latest or {}).get("rationale") or "").strip()

    b1, b2, rest = st.columns([0.7, 0.7, 5.5], gap="small")
    with b1:
        up_type = "primary" if saved_vote == "up" and not is_editing_down else "secondary"
        if st.button(
            "👍",
            key=f"up_{item_id}",
            help="Relevant (saved for everyone)",
            use_container_width=True,
            type=up_type,
        ):
            st.session_state[pending_key] = None
            queue_action(
                "feedback_up",
                {
                    "item_id": item_id,
                    "score": float(item.get("relevance_score") or 0),
                },
            )
    with b2:
        down_selected = saved_vote == "down" or is_editing_down
        # Marker scopes red styling to this column; primary = selected (red), secondary = idle.
        st.markdown('<span class="ci-fb-down-slot">down</span>', unsafe_allow_html=True)
        if st.button(
            "👎",
            key=f"down_{item_id}",
            help="Not relevant (saved for everyone)",
            use_container_width=True,
            type="primary" if down_selected else "secondary",
        ):
            st.session_state[pending_key] = item_id
            st.session_state[f"rationale_{item_id}"] = saved_rationale
            st.rerun()

    with rest:
        if saved_vote == "up" and not is_editing_down:
            st.caption("Saved feedback: 👍 relevant (shared)")
        elif saved_vote == "down" and not is_editing_down:
            note = saved_rationale or "No rationale provided"
            st.caption(f"Saved feedback: 👎 not relevant — {note}")
            if st.button("Edit rationale", key=f"edit_fb_{item_id}"):
                st.session_state[pending_key] = item_id
                st.session_state[f"rationale_{item_id}"] = saved_rationale
                st.rerun()
        elif is_editing_down:
            rationale_key = f"rationale_{item_id}"
            if rationale_key not in st.session_state:
                st.session_state[rationale_key] = saved_rationale
            rationale = st.text_input(
                "Why is this not relevant?",
                key=rationale_key,
                placeholder="e.g. Not relevant because…",
            )
            s1, s2 = st.columns([1.2, 1])
            with s1:
                if st.button("Save feedback", key=f"save_fb_{item_id}", type="primary"):
                    queue_action(
                        "feedback_down",
                        {
                            "item_id": item_id,
                            "score": float(item.get("relevance_score") or 0),
                            "rationale": rationale,
                        },
                    )
            with s2:
                if st.button("Cancel", key=f"cancel_fb_{item_id}"):
                    st.session_state[pending_key] = None
                    st.rerun()


DIGEST_PAGE_SIZE = 10
DIGEST_SORT_LABELS = {
    "Creation date": "date",
    "Relevance": "relevance",
}


def _on_digest_sort_change() -> None:
    # Callback runs before widgets render — safe to reset both keys.
    st.session_state.digest_page = 1
    st.session_state.digest_page_select = 1


def _on_digest_news_date_change() -> None:
    st.session_state.digest_page = 1
    st.session_state.digest_page_select = 1


def _on_digest_page_jump() -> None:
    st.session_state.digest_page = int(st.session_state.digest_page_select)


def _digest_sort_key() -> str:
    label = st.session_state.get("digest_sort_label", "Creation date")
    return DIGEST_SORT_LABELS.get(label, "date")


NEWS_DATE_LOOKBACK_DAYS = 90


def _news_date_bounds(all_items: list) -> tuple[date, date]:
    """Calendar range that always allows picking days other than today.

    Streamlit locks ``date_input`` when min_value == max_value; keep a lookback
    window even when every item was ingested on the same day.
    """
    today = israel_today()
    days = [d for item in all_items if (d := item_news_date(item)) is not None]
    earliest = min(days) if days else today
    min_day = min(earliest, today - timedelta(days=NEWS_DATE_LOOKBACK_DAYS))
    max_day = max(max(days) if days else today, today)
    return min_day, max_day


def _latest_day_with_items(all_items: list) -> date | None:
    days = [d for item in all_items if (d := item_news_date(item)) is not None]
    return max(days) if days else None


def _render_news_date_filter(all_items: list) -> tuple[list, str | None]:
    """Single-day calendar picker; defaults to Israel today, else latest day with items."""
    today = israel_today()
    min_day, max_day = _news_date_bounds(all_items)
    note: str | None = None
    today_items = filter_digest_by_news_dates(all_items, [today])
    fallback_day = _latest_day_with_items(all_items)

    if "digest_news_date" not in st.session_state:
        if today_items:
            st.session_state.digest_news_date = today
        elif fallback_day is not None:
            st.session_state.digest_news_date = fallback_day
            note = (
                f"No items for today ({today.isoformat()} Israel). "
                f"Showing the most recent day with news: {fallback_day.isoformat()}."
            )
        else:
            st.session_state.digest_news_date = today
    else:
        current = st.session_state.digest_news_date
        if isinstance(current, date):
            if current < min_day:
                st.session_state.digest_news_date = min_day
            elif current > max_day:
                st.session_state.digest_news_date = max_day
        else:
            st.session_state.digest_news_date = today

    st.markdown(
        '<div class="ci-digest-date-filter-marker"></div>'
        '<div class="ci-digest-field-label">News date</div>',
        unsafe_allow_html=True,
    )
    selected = st.date_input(
        "News date",
        key="digest_news_date",
        min_value=min_day,
        max_value=max_day,
        on_change=_on_digest_news_date_change,
        help="Show items ingested on this calendar day (Israel timezone). Default: today.",
        label_visibility="collapsed",
        format="DD/MM/YYYY",
    )
    if not isinstance(selected, date):
        selected = today
    if (
        note is None
        and selected == today
        and not today_items
        and fallback_day is not None
        and st.session_state.get("_digest_auto_date_note_shown")
    ):
        note = st.session_state.get("_digest_auto_date_note")
    if note:
        st.session_state["_digest_auto_date_note"] = note
        st.session_state["_digest_auto_date_note_shown"] = True
        st.caption(note)
    return filter_digest_by_news_dates(all_items, [selected]), note


def _render_digest_controls(total: int) -> tuple[int, int]:
    """Sort + pagination on one desktop row. Returns (start, end) slice indices."""
    total_pages = max(1, (total + DIGEST_PAGE_SIZE - 1) // DIGEST_PAGE_SIZE)
    if "digest_page" not in st.session_state:
        st.session_state.digest_page = 1

    page = min(max(1, int(st.session_state.digest_page)), total_pages)
    st.session_state.digest_page = page
    # Sync widget key BEFORE the selectbox is instantiated (required by Streamlit).
    st.session_state.digest_page_select = page

    start = (page - 1) * DIGEST_PAGE_SIZE
    end = min(start + DIGEST_PAGE_SIZE, total)
    showing = f"Showing {start + 1}–{end} of {total}" if total else "Showing 0 of 0"

    sort_c, meta_c, prev_c, jump_c, next_c = st.columns(
        [1.65, 1.45, 1.0, 1.35, 1.0],
        gap="small",
    )
    with sort_c:
        st.markdown(
            '<div class="ci-digest-controls-marker"></div>'
            '<div class="ci-digest-field-label">Sort by</div>',
            unsafe_allow_html=True,
        )
        st.selectbox(
            "Sort by",
            options=list(DIGEST_SORT_LABELS.keys()),
            key="digest_sort_label",
            on_change=_on_digest_sort_change,
            help="Default: newest creation date first",
            label_visibility="collapsed",
        )
    with meta_c:
        st.markdown(
            f'<div class="ci-digest-meta">{showing}</div>',
            unsafe_allow_html=True,
        )
    with prev_c:
        if st.button(
            "← Previous",
            disabled=page <= 1,
            use_container_width=True,
            key="digest_prev",
        ):
            # Only touch digest_page here — select key is synced on the next run.
            st.session_state.digest_page = page - 1
            st.rerun()
    with jump_c:
        st.selectbox(
            "Jump to page",
            options=list(range(1, total_pages + 1)),
            key="digest_page_select",
            format_func=lambda n: f"Page {n} of {total_pages}",
            label_visibility="collapsed",
            help="Jump to page",
            on_change=_on_digest_page_jump,
        )
    with next_c:
        if st.button(
            "Next →",
            disabled=page >= total_pages,
            use_container_width=True,
            key="digest_next",
        ):
            st.session_state.digest_page = page + 1
            st.rerun()

    page = int(st.session_state.digest_page)
    start = (page - 1) * DIGEST_PAGE_SIZE
    end = min(start + DIGEST_PAGE_SIZE, total)
    return start, end


def _render_digest_tab(repo: Repository, db_path: Path | None) -> None:
    if "applied_weights" not in st.session_state:
        st.session_state.applied_weights = dict(get_effective_weights(repo))
    if "digest_sort_label" not in st.session_state:
        st.session_state.digest_sort_label = "Creation date"

    _weight_editor_fragment(repo)

    dig_l, dig_r = st.columns([4, 1])
    with dig_l:
        st.markdown('<div class="ci-section-title">Daily digest</div>', unsafe_allow_html=True)
    with dig_r:
        st.markdown('<span class="ci-run-now-slot">run</span>', unsafe_allow_html=True)
        if st.button(
            "Run Now",
            type="secondary",
            use_container_width=True,
            key="run_now_digest",
        ):
            queue_action("run_now")

    sort_by = _digest_sort_key()
    try:
        all_items = list_digest(
            repo,
            weight_overrides=st.session_state.applied_weights,
            sort_by=sort_by,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        if not is_connection_error(exc):
            raise
        mark_db_dirty()
        repo, db_path = get_repository()
        all_items = list_digest(
            repo,
            weight_overrides=st.session_state.applied_weights,
            sort_by=sort_by,  # type: ignore[arg-type]
        )

    if not all_items:
        st.info("No news items yet. Use **Run Now** to ingest live data.")
        if st.button("Run Now", type="primary", key="run_now_empty"):
            queue_action("run_now")
        return

    items, _date_note = _render_news_date_filter(all_items)

    filt_l, filt_r = st.columns([1.2, 1.5])
    with filt_l:
        type_options = ["All types", "competitor", "emerging", "industry"]
        selected_type = st.selectbox(
            "Item type",
            type_options,
            key="digest_item_type",
            on_change=_on_digest_news_date_change,
        )
    with filt_r:
        min_relevance = st.slider(
            "Minimum relevance",
            min_value=0.0,
            max_value=5.0,
            value=2.5,
            step=0.1,
            key="digest_min_relevance",
            help=(
                "Hide items below this score. Fallback / Not scored items are "
                "hidden by default when the minimum is above 0."
            ),
            on_change=_on_digest_news_date_change,
        )

    if selected_type != "All types":
        items = [i for i in items if (i.get("item_type") or "") == selected_type]

    # Default min 2.5 hides low scores and fallback (no score) from the view.
    visible: list = []
    for item in items:
        if item.get("is_fallback") or item.get("relevance_score") is None:
            if min_relevance <= 0:
                visible.append(item)
            continue
        if float(item["relevance_score"]) >= float(min_relevance):
            visible.append(item)
    items = visible

    _render_kpis(digest_kpis(repo, items))

    if not items:
        st.info(
            "No news for this filter. "
            "Try another date, lower Minimum relevance, or run ingestion."
        )
        return

    start, end = _render_digest_controls(len(items))
    for item in items[start:end]:
        st.markdown(render_news_card(item), unsafe_allow_html=True)
        _render_feedback_row(repo, item)


def _ask_user_turn_count(thread: list[dict]) -> int:
    return sum(1 for t in thread if t.get("role") == "user")


def _render_ask_tab(repo: Repository) -> None:
    from src.services.ask_digest import MAX_FOLLOW_UPS, MAX_USER_TURNS

    st.markdown('<div class="ci-section-title">Ask the digest</div>', unsafe_allow_html=True)
    st.caption(
        "Light RAG over retrieved news **plus** the curated comparison matrix. "
        f"Short chat memory in this browser session only — up to **{MAX_FOLLOW_UPS}** "
        "follow-ups per thread (token guardrail). Not a persistent model memory."
    )

    thread: list[dict] = list(st.session_state.get("_ci_ask_thread") or [])
    user_turns = _ask_user_turn_count(thread)
    follow_ups_used = max(0, user_turns - 1) if user_turns else 0
    can_follow_up = user_turns > 0 and user_turns < MAX_USER_TURNS

    if thread:
        st.markdown("**Conversation**")
        for turn in thread:
            role = "You" if turn.get("role") == "user" else "Assistant"
            st.markdown(f"**{role}:** {turn.get('content') or ''}")
        result = st.session_state.get("_ci_ask_result") or {}
        citations = result.get("citations") or []
        if citations:
            st.markdown("**News sources (latest turn)**")
            for i, c in enumerate(citations, start=1):
                st.markdown(
                    f"[{i}] [{c.get('title')}]({c.get('url')}) — {c.get('competitor')}"
                )
        if result.get("used_comparison"):
            st.caption("Also used curated Comparison matrix claims (see Comparison tab).")
        st.caption(
            f"Follow-ups used: {follow_ups_used}/{MAX_FOLLOW_UPS}. "
            "Start a new chat to reset."
        )

    if user_turns >= MAX_USER_TURNS:
        st.info(
            f"This thread hit the {MAX_FOLLOW_UPS}-follow-up limit. "
            "Click **New chat** to continue with a fresh context."
        )
        if st.button("New chat", key="ask_new_chat_limit", use_container_width=True):
            st.session_state.pop("_ci_ask_thread", None)
            st.session_state.pop("_ci_ask_result", None)
            st.rerun()
        return

    placeholder = (
        "Follow-up, e.g. How does that compare to JFrog Artifactory?"
        if can_follow_up
        else "e.g. What did Snyk announce that matters to JFrog?"
    )
    question = st.text_input(
        "Follow-up" if can_follow_up else "Question",
        placeholder=placeholder,
        key="ask_question_input",
    )
    c1, c2 = st.columns([3, 1])
    with c1:
        ask_label = "Ask follow-up" if can_follow_up else "Ask"
        ask_clicked = st.button(ask_label, type="primary", use_container_width=True)
    with c2:
        if thread and st.button("New chat", key="ask_new_chat", use_container_width=True):
            st.session_state.pop("_ci_ask_thread", None)
            st.session_state.pop("_ci_ask_result", None)
            st.rerun()

    st.markdown('<div class="ci-bottom-spacer"></div>', unsafe_allow_html=True)

    if ask_clicked and question.strip():
        queue_action(
            "ask",
            {
                "question": question.strip(),
                "history": [
                    {"role": t["role"], "content": t["content"]} for t in thread
                ],
            },
        )
    elif ask_clicked:
        flash("Enter a question first", ok=False)
        st.rerun()


def _render_comparison_tab() -> None:
    st.markdown(
        '<div class="ci-section-title">JFrog vs competitors</div>',
        unsafe_allow_html=True,
    )
    company_order, rows, notes, meta = get_comparison_matrix()
    reviewed = meta.get("last_reviewed") or "unknown"
    st.caption(
        f"Curated matrix (not live news). Last reviewed: **{reviewed}**. "
        "Analysts update `config/comparison.yaml` when product pages change."
    )
    st.markdown(
        render_comparison_matrix(company_order, rows, competitor_labels()),
        unsafe_allow_html=True,
    )
    if notes:
        st.markdown("**Context notes**")
        for note in notes:
            st.markdown(f"- {note}")


def _render_runs_tab(repo: Repository) -> None:
    st.markdown('<div class="ci-section-title">Pipeline run history</div>', unsafe_allow_html=True)
    st.caption("Cron and manual ingestion runs. Times shown in Israel timezone.")
    st.markdown(render_run_history(repo.list_runs(limit=30)), unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(
        page_title="CI Intel | JFrog",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_css()
    flash_msg = pop_flash()

    st.markdown(
        render_banner(
            "CI",
            " Intel",
            "Daily competitive intelligence digest and sourced comparison matrix.",
        ),
        unsafe_allow_html=True,
    )

    try:
        repo, db_path = get_repository()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not open database: {exc}")
        st.stop()

    handle_pending_action(repo, db_path, run_pipeline=_try_run_pipeline)

    tab_digest, tab_ask, tab_compare, tab_runs = st.tabs(
        ["Daily Digest", "Ask the Digest", "Comparison", "Pipeline runs"]
    )
    with tab_digest:
        _render_digest_tab(repo, db_path)
    with tab_ask:
        _render_ask_tab(repo)
    with tab_compare:
        _render_comparison_tab()
    with tab_runs:
        _render_runs_tab(repo)

    boot_host_chrome(flash=flash_msg)


if __name__ == "__main__":
    main()
