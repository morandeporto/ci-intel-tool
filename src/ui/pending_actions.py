"""Two-step async UX: show bottom loader, then run work, then toast."""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.db.repository import Repository
from src.process.llm_classify import ClassifyError
from src.services.ask_digest import ChatTurn, ask_digest
from src.services.feedback import record_feedback
from src.services.weights import save_weights
from src.ui.db_session import mark_db_dirty, invalidate_db_cache
from src.ui.host_chrome import (
    clear_busy_toast,
    schedule_continue_click,
    show_busy_toast,
)

FLASH_KEY = "_ci_flash"
PENDING_KEY = "_ci_pending"
BUSY_KEY = "_ci_busy_label"
DO_WORK_KEY = "_ci_do_work"

ACTION_LABELS = {
    "save_weights": "Saving weights…",
    "feedback_up": "Saving feedback…",
    "feedback_down": "Saving feedback…",
    "ask": "Asking the digest…",
}


def flash(message: str, *, ok: bool = True) -> None:
    st.session_state[FLASH_KEY] = {"msg": message, "ok": ok}


def pop_flash() -> dict | None:
    return st.session_state.pop(FLASH_KEY, None)


def queue_action(name: str, payload: dict | None = None) -> None:
    st.session_state[PENDING_KEY] = {"name": name, "payload": payload or {}}
    st.session_state[BUSY_KEY] = ACTION_LABELS.get(name, "Working…")
    st.session_state.pop(DO_WORK_KEY, None)
    st.rerun()


def _execute(
    name: str,
    payload: dict[str, Any],
    repo: Repository,
) -> tuple[bool, str]:
    if name == "save_weights":
        saved = save_weights(repo, {k: float(v) for k, v in payload["weights"].items()})
        st.session_state.applied_weights = saved
        return True, "Weights saved successfully"
    if name == "feedback_up":
        record_feedback(
            repo,
            news_item_id=str(payload["item_id"]),
            original_score=float(payload["score"]),
            vote="up",
            rationale=None,
        )
        st.session_state["pending_down_id"] = None
        return True, "Feedback saved (👍)"
    if name == "feedback_down":
        record_feedback(
            repo,
            news_item_id=str(payload["item_id"]),
            original_score=float(payload["score"]),
            vote="down",
            rationale=payload.get("rationale"),
        )
        st.session_state["pending_down_id"] = None
        return True, "Feedback saved (👎)"
    if name == "ask":
        history_raw = payload.get("history") or []
        history = [
            ChatTurn(role=str(t["role"]), content=str(t["content"]))
            for t in history_raw
            if isinstance(t, dict) and t.get("role") and t.get("content")
        ]
        question = str(payload["question"])
        result = ask_digest(repo, question, history=history)
        prior = list(st.session_state.get("_ci_ask_thread") or [])
        # Citations live on the assistant turn so each answer's [1]/[n]
        # matches the Sources list rendered directly under that turn.
        turn_citations = [
            {"title": c.title, "url": c.url, "competitor": c.competitor}
            for c in (result.citations or [])
        ]
        turn_matrix = [
            {
                "mid": m.mid,
                "label": f"{m.company_label} — {m.capability_label}",
                "claim": m.claim,
                "url": m.source_url,
            }
            for m in (result.matrix_citations or [])
        ]
        thread = prior + [
            {"role": "user", "content": question},
            {
                "role": "assistant",
                "content": result.answer,
                "citations": turn_citations,
                "matrix_citations": turn_matrix,
                "used_comparison": bool(result.used_comparison),
            },
        ]
        st.session_state["_ci_ask_thread"] = thread
        st.session_state["_ci_ask_result"] = {
            "answer": result.answer,
            "citations": turn_citations,
            "matrix_citations": turn_matrix,
            "used_comparison": bool(result.used_comparison),
            "model_id": result.model_id,
        }
        # Clear the input for the next follow-up.
        st.session_state["ask_question_input"] = ""
        # Stay on Ask after the post-work rerun (radio nav, not st.tabs).
        st.session_state["_ci_main_tab"] = "Ask the Digest"
        return True, "Answer ready"
    return False, f"Unknown action: {name}"


def handle_pending_action(repo: Repository) -> None:
    pending = st.session_state.get(PENDING_KEY)
    busy_label = st.session_state.get(BUSY_KEY)
    if not pending or not busy_label:
        return

    show_busy_toast(str(busy_label))

    if st.session_state.get(DO_WORK_KEY):
        try:
            ok, message = _execute(
                str(pending.get("name") or ""),
                pending.get("payload") or {},
                repo,
            )
        except ClassifyError as exc:
            st.session_state.pop("_ci_ask_result", None)
            ok, message = False, str(exc)
        except Exception as exc:  # noqa: BLE001
            st.session_state.pop("_ci_ask_result", None)
            ok, message = False, str(exc)

        st.session_state.pop(PENDING_KEY, None)
        st.session_state.pop(BUSY_KEY, None)
        st.session_state.pop(DO_WORK_KEY, None)
        mark_db_dirty()
        invalidate_db_cache()
        clear_busy_toast()
        flash(message, ok=ok)
        st.rerun()

    # Only the continue control is rendered before st.stop() - hide it off-screen.
    st.markdown(
        '<style>div[data-testid="stButton"] { position:fixed!important;left:-10000px!important;'
        "width:1px!important;height:1px!important;opacity:0!important; }</style>",
        unsafe_allow_html=True,
    )
    if st.button("ci_continue", key="_ci_pending_continue", type="secondary"):
        st.session_state[DO_WORK_KEY] = True
        st.rerun()

    schedule_continue_click()
    st.stop()
