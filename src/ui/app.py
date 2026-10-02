"""Streamlit entrypoint for the CI Intel Tool.

Run from repo root:
  .venv/bin/streamlit run src/ui/app.py

Design tokens emulate a JFrog-like dark aesthetic; no official logos are used.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Ensure repo root is on sys.path when launched via `streamlit run src/ui/app.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.connection import (
    SEED_DB_PATH,
    db_label,
    open_repo_connection,
    turso_configured,
)
from src.db.models import DIMENSION_NAMES
from src.db.repository import Repository
from src.process.llm_classify import ClassifyError
from src.services.ask_digest import ask_digest
from src.services.comparison import get_comparison_matrix
from src.services.digest import digest_kpis, list_digest
from src.services.feedback import record_feedback
from src.services.weights import get_effective_weights, save_weights, weights_meta
from src.ui.components import (
    competitor_labels,
    load_styles,
    render_banner,
    render_comparison_matrix,
    render_kpi_row,
    render_news_card,
    render_run_history,
)

CSS_PATH = Path(__file__).resolve().parent / "styles.css"
DIM_LABELS = {
    "jfrog_relevance": "JFrog relevance",
    "competitor_signal": "Competitor signal",
    "strategic_impact": "Strategic impact",
    "freshness": "Freshness",
    "market_visibility": "Market visibility",
}


def _inject_css() -> None:
    if "ci_css_loaded" not in st.session_state:
        st.session_state.ci_css_loaded = True
    css = load_styles(CSS_PATH)
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


@st.cache_resource
def _cached_connection(cache_key: str):
    """Cache key changes when switching Turso ↔ local so connections refresh."""
    conn, path = open_repo_connection()
    return conn, path


def _get_repo() -> tuple[Repository, Path | None]:
    if turso_configured():
        cache_key = "turso"
    else:
        from src.db.connection import resolve_db_path

        cache_key = f"local:{resolve_db_path()}"
    conn, path = _cached_connection(cache_key)
    return Repository(conn), path


WEIGHT_SUM_TOLERANCE = 0.01


def _weight_sum_status(total: float) -> tuple[str, str]:
    """Return (level, message) for the live weight-sum indicator."""
    if total > 1.0 + WEIGHT_SUM_TOLERANCE:
        return (
            "error",
            f"Sum is **{total:.2f}** — must be ≤ 1.00. Lower one or more sliders before saving.",
        )
    if abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE:
        return ("ok", f"Sum is **{total:.2f}** / 1.00 — ready to save.")
    # 0 < total < 1
    remaining = 1.0 - total
    return (
        "warn",
        f"Sum is **{total:.2f}** / 1.00 — still **{remaining:.2f}** short. "
        "Raise sliders until the sum reaches 1.00.",
    )


def _try_run_pipeline(db_path: Path | None) -> tuple[bool, str]:
    """Invoke live ingestion; cap items to control Gemini cost from the UI."""
    try:
        from src.config_loader import DEFAULT_DB_PATH
        from src.pipeline.run_daily import run_daily
    except ImportError:
        return (
            False,
            "Pipeline module is not available (src.pipeline.run_daily). "
            "Seed data still powers the demo.",
        )
    try:
        # Prefer Turso when configured; otherwise always write to runtime DB
        # (never mutate the committed seed.db from the UI).
        # WHY limit=1 during demos: one Gemini call max from the UI button.
        target = None if turso_configured() else DEFAULT_DB_PATH
        result = run_daily(trigger="manual", db_path=target, limit=1)
        return (
            True,
            f"Pipeline {result.status}: fetched={result.items_fetched}, "
            f"new={result.items_new}, scored={result.items_scored}"
            + (f" — {result.message}" if result.message else ""),
        )
    except Exception as exc:  # noqa: BLE001 — surface clean UI message
        return False, f"Pipeline failed: {exc}"


@st.fragment
def _weight_editor_fragment(repo: Repository) -> None:
    """Edit draft weights with a live sum. Digest re-ranks only after a valid Save.

    Uses a fragment so slider ticks do not rebuild the whole news list.
    """
    current = st.session_state.applied_weights
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
    level, message = _weight_sum_status(total)
    # Visual meter: green at 1.0, amber under, red over.
    pct = min(total / 1.0, 1.25)  # allow bar to show overflow a bit
    bar_color = {"ok": "#40BE46", "warn": "#E6A23C", "error": "#E74C3C"}[level]
    st.markdown(
        f"""
        <div style="margin:0.4rem 0 0.6rem 0;">
          <div style="display:flex;justify-content:space-between;color:#8C9FA4;font-size:0.85rem;">
            <span>Weight sum</span><span>{total:.2f} / 1.00</span>
          </div>
          <div style="height:8px;background:#1B2147;border-radius:9999px;overflow:hidden;">
            <div style="width:{min(pct,1.0)*100:.1f}%;height:100%;background:{bar_color};"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if level == "ok":
        st.success(message)
    elif level == "warn":
        st.warning(message)
    else:
        st.error(message)

    can_save = abs(total - 1.0) <= WEIGHT_SUM_TOLERANCE
    if st.button("Save weights", type="primary", disabled=not can_save, use_container_width=True):
        try:
            # Exact values as set — no silent re-normalization that "scrambles" ratios.
            saved = save_weights(repo, {k: float(v) for k, v in raw_weights.items()})
            st.session_state.applied_weights = saved
            st.success("Weights saved. Reloading digest ranking…")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not save weights: {exc}")


def _render_digest_tab(repo: Repository, db_path: Path | None) -> None:
    # Applied weights drive ranking. Draft slider edits are local until Save.
    if "applied_weights" not in st.session_state:
        st.session_state.applied_weights = dict(get_effective_weights(repo))
    meta = weights_meta(repo)

    st.markdown('<div class="ci-section-title">Weight tuning</div>', unsafe_allow_html=True)
    st.caption(
        "Drag sliders until the sum equals **1.00**, then Save. "
        "Saving is blocked if the sum is over 1.00 or not yet complete. "
        "The digest re-ranks only after a successful save (no Gemini call)."
    )
    if meta.get("updated_at"):
        st.caption(f"Saved weights source: {meta['source']} · updated {meta['updated_at']}")

    _weight_editor_fragment(repo)

    applied = st.session_state.applied_weights
    st.caption(
        "Active ranking weights: "
        + ", ".join(f"{DIM_LABELS[k]}={float(applied[k]):.2f}" for k in DIMENSION_NAMES)
    )

    run_col, info_col = st.columns([1, 3])
    with run_col:
        if st.button("Run Now", type="primary", use_container_width=True):
            ok, msg = _try_run_pipeline(db_path)
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.warning(msg)
    with info_col:
        st.caption(db_label(db_path))
        st.caption(
            "Token saver: prefer the seeded Turso demo. Run Now calls Gemini "
            "(capped to 1 item). Avoid Ask the Digest unless you want an API call."
        )

    # Rank only with last-saved/applied weights — not live slider drafts.
    items = list_digest(repo, weight_overrides=applied)
    kpis = digest_kpis(repo, items)
    avg = f"{kpis['avg_score']:.2f}" if kpis["avg_score"] is not None else "—"
    st.markdown(
        render_kpi_row(
            [
                (str(kpis["item_count"]), "News items", False),
                (avg, "Avg relevance", True),
                (str(kpis["high_score_count"]), "High scores (≥4)", False),
                (str(kpis["feedback_count"]), "Feedback signals", False),
            ]
        ),
        unsafe_allow_html=True,
    )

    st.markdown('<div class="ci-section-title">Daily digest</div>', unsafe_allow_html=True)
    if not items:
        st.warning(
            "No news items found. Seed data should be loaded into Turso, or run "
            "`python scripts/seed_db.py` for local demo."
        )
    else:
        for item in items:
            st.markdown(render_news_card(item), unsafe_allow_html=True)
            fb_cols = st.columns([1, 1, 4, 1])
            rationale_key = f"rationale_{item['id']}"
            with fb_cols[0]:
                if st.button("👍", key=f"up_{item['id']}", help="Mark relevant"):
                    try:
                        record_feedback(
                            repo,
                            news_item_id=item["id"],
                            original_score=float(item.get("relevance_score") or 0),
                            vote="up",
                            rationale=st.session_state.get(rationale_key),
                        )
                        st.toast("Feedback saved (👍)")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not save feedback: {exc}")
            with fb_cols[1]:
                if st.button("👎", key=f"down_{item['id']}", help="Mark not relevant"):
                    try:
                        record_feedback(
                            repo,
                            news_item_id=item["id"],
                            original_score=float(item.get("relevance_score") or 0),
                            vote="down",
                            rationale=st.session_state.get(rationale_key),
                        )
                        st.toast("Feedback saved (👎)")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not save feedback: {exc}")
            with fb_cols[2]:
                st.text_input(
                    "Rationale (optional)",
                    key=rationale_key,
                    placeholder="e.g. Not relevant because…",
                    label_visibility="collapsed",
                )

    st.markdown('<div class="ci-section-title">Pipeline run history</div>', unsafe_allow_html=True)
    runs = repo.list_runs(limit=15)
    st.markdown(render_run_history(runs), unsafe_allow_html=True)


def _render_ask_tab(repo: Repository) -> None:
    st.markdown('<div class="ci-section-title">Ask the digest</div>', unsafe_allow_html=True)
    st.caption(
        "Light RAG: retrieve relevant news from our database, then ask Gemini to answer "
        "using only those items (with citations). Each Ask = 1 Gemini call — skip while "
        "conserving free-tier tokens; use Daily Digest + Comparison for the demo."
    )
    question = st.text_input(
        "Question",
        placeholder="e.g. What did Snyk announce that matters to JFrog?",
    )
    if st.button("Ask", type="primary") and question.strip():
        with st.spinner("Retrieving digest items and asking Gemini…"):
            try:
                result = ask_digest(repo, question.strip())
            except ClassifyError as exc:
                st.error(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                st.error(f"Ask failed: {exc}")
                return
        st.markdown(result.answer)
        if result.citations:
            st.markdown("**Retrieved sources**")
            for i, c in enumerate(result.citations, start=1):
                st.markdown(f"[{i}] [{c.title}]({c.url}) — {c.competitor}")
        st.caption(f"Model: {result.model_id}")


def _render_comparison_tab() -> None:
    st.markdown(
        '<div class="ci-section-title">JFrog vs competitors</div>',
        unsafe_allow_html=True,
    )
    company_order, rows, notes, meta = get_comparison_matrix()
    reviewed = meta.get("last_reviewed") or "unknown"
    by = meta.get("reviewed_by") or "curator"
    st.caption(
        f"Curated capability matrix (not live news). Last reviewed: **{reviewed}** "
        f"by {by}. New digest headlines do **not** auto-update these cells — "
        "a CI analyst edits `config/comparison.yaml` when official product pages change."
    )
    labels = competitor_labels()
    st.markdown(
        render_comparison_matrix(company_order, rows, labels),
        unsafe_allow_html=True,
    )
    if notes:
        st.markdown('<div class="ci-panel">', unsafe_allow_html=True)
        st.markdown("**Context notes**")
        for note in notes:
            st.markdown(f"- {note}")
        st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(
        page_title="CI Intel | JFrog",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_css()

    st.markdown(
        render_banner(
            "CI",
            " Intel",
            "Daily competitive intelligence digest and sourced comparison matrix.",
        ),
        unsafe_allow_html=True,
    )

    try:
        repo, db_path = _get_repo()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not open database: {exc}")
        st.stop()

    tab_digest, tab_ask, tab_compare = st.tabs(
        ["Daily Digest", "Ask the Digest", "Comparison"]
    )
    with tab_digest:
        _render_digest_tab(repo, db_path)
    with tab_ask:
        _render_ask_tab(repo)
    with tab_compare:
        _render_comparison_tab()


if __name__ == "__main__":
    main()
