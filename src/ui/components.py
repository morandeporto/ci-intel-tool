"""Safe HTML helpers for the Streamlit UI (all dynamic text is escaped)."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.config_loader import load_competitors

SCORE_MAX = 5.0
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def _e(value: Any) -> str:
    """Escape untrusted / LLM / user text for unsafe_allow_html rendering."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def format_israel_time(value: str | None) -> str:
    """Format ISO timestamps as clean Israel local time: DD/MM/YYYY HH:MM."""
    if not value:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ISRAEL_TZ)
        return local.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        # Already a short date like YYYY-MM-DD
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            try:
                d = datetime.strptime(text, "%Y-%m-%d")
                return d.strftime("%d/%m/%Y")
            except ValueError:
                return _e(text)
        return _e(text)


def competitor_colors() -> dict[str, str]:
    return {c["id"]: c.get("accent_color", "#333642") for c in load_competitors()}


def competitor_labels() -> dict[str, str]:
    return {c["id"]: c.get("display_name", c["id"]) for c in load_competitors()}


def load_styles(css_path) -> str:
    return css_path.read_text(encoding="utf-8")


def render_banner(title_green: str, title_white: str, subtitle: str) -> str:
    return f"""
    <div class="ci-banner">
      <h1><span class="ci-half-green">{_e(title_green)}</span>
      <span class="ci-half-white">{_e(title_white)}</span></h1>
      <p>{_e(subtitle)}</p>
    </div>
    """


def render_kpi(value: str, label: str, *, glow: bool = False) -> str:
    """Single compact KPI card (render one per Streamlit column — avoids HTML breakups)."""
    glow_cls = " glow" if glow else ""
    return f"""
    <div class="ci-kpi">
      <div class="ci-kpi-value{glow_cls}">{_e(value)}</div>
      <div class="ci-kpi-label">{_e(label)}</div>
    </div>
    """


DIM_CHIP_LABELS = {
    "jfrog_relevance": "JFrog relevance",
    "competitor_signal": "Market and competitive pressure",
    "strategic_impact": "Strategic impact",
    "freshness": "Freshness",
    "market_visibility": "Market visibility",
}


def render_not_scored_badge() -> str:
    return '<div class="ci-score-wrap"><span class="ci-badge ci-badge-not-scored">Not scored</span></div>'


def render_score_ring(score: float | None, *, is_fallback: bool = False) -> str:
    if is_fallback or score is None:
        return render_not_scored_badge()
    pct = max(0, min(100, int(round((float(score) / SCORE_MAX) * 100))))
    label = f"{float(score):.2f}"
    # Inline conic-gradient (no CSS vars) — Streamlit sanitizers often strip `--pct`.
    ring_bg = f"background:conic-gradient(#40BE46 {pct}%,#333642 0);"
    return (
        f'<div class="ci-score-wrap">'
        f'<div class="ci-score-ring" style="{ring_bg}">'
        f'<div class="ci-score-ring-inner">{_e(label)}</div>'
        f"</div>"
        f'<div class="ci-score-label">relevance</div>'
        f"</div>"
    )


def render_item_type_badge(item_type: str | None) -> str:
    if not item_type:
        return ""
    return (
        f'<span class="ci-badge ci-badge-item-type">{_e(str(item_type))}</span>'
    )


def render_competitor_badge(competitor_id: str) -> str:
    colors = competitor_colors()
    labels = competitor_labels()
    color = colors.get(competitor_id, "#333642")
    label = labels.get(competitor_id, competitor_id.replace("_", " ").title())
    if competitor_id == "industry":
        color = "#333642"
        label = "Industry"
    return (
        f'<span class="ci-badge" style="background:{_e(color)};">'
        f"{_e(label)}</span>"
    )


def render_category_badge(category: str | None) -> str:
    if not category:
        return ""
    return f'<span class="ci-badge ci-badge-category">{_e(category)}</span>'


def render_news_card(item: dict[str, Any]) -> str:
    """Render a digest news card. Dynamic fields are HTML-escaped.

    Important: keep HTML compact (no blank lines). Streamlit's markdown
    parser splits on blank lines and breaks nested card structure.
    """
    title = _e(item.get("title") or "Untitled")
    summary = _e(item.get("summary") or "")
    url = item.get("url") or ""
    safe_href = ""
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        safe_href = html.escape(url, quote=True)
    competitor = item.get("competitor") or "industry"
    category = item.get("category")
    item_type = item.get("item_type")
    implication = item.get("jfrog_implication") or ""
    is_fallback = bool(item.get("is_fallback"))
    score = item.get("relevance_score")
    published = format_israel_time(
        item.get("published_at") or item.get("ingested_at")
    )

    dims = item.get("dimensions") or {}
    dim_html = ""
    if dims and not is_fallback:
        chips = "".join(
            f'<span class="ci-dim-chip">'
            f"{_e(DIM_CHIP_LABELS.get(k, k.replace('_', ' ')))}: {_e(v)}</span>"
            for k, v in dims.items()
        )
        dim_html = f'<div class="ci-dims">{chips}</div>'

    implication_html = ""
    if implication:
        implication_html = (
            f'<p class="ci-card-implication"><span class="ci-implication-label">'
            f"JFrog implication:</span> {_e(implication)}</p>"
        )

    link_html = ""
    if safe_href:
        link_html = (
            f'<a class="ci-link" href="{safe_href}" target="_blank" rel="noopener noreferrer">'
            f"Source <span class=\"ci-chevron\">›</span></a>"
        )

    score_html = render_score_ring(
        float(score) if score is not None else None,
        is_fallback=is_fallback,
    )
    # Single-line outer structure so Streamlit does not fragment the card.
    return (
        f'<div class="ci-card">'
        f'<div class="ci-card-score">{score_html}</div>'
        f'<div class="ci-card-main">'
        f"{render_competitor_badge(str(competitor))}"
        f"{render_item_type_badge(str(item_type) if item_type else None)}"
        f"{render_category_badge(category)}"
        f'<h3 class="ci-card-title">{title}</h3>'
        f'<p class="ci-card-summary">{summary}</p>'
        f"{implication_html}"
        f"{link_html}"
        f'<div class="ci-muted ci-card-date">{_e(published)} (Israel)</div>'
        f"{dim_html}"
        f"</div></div>"
    )


def _claim_cell_html(claim) -> str:
    if claim.is_unknown:
        return '<span class="ci-unknown">Unknown</span>'
    quote_html = (
        f'<div class="ci-quote">“{_e(claim.quote)}”</div>' if claim.quote else ""
    )
    link_html = ""
    if claim.source_url and claim.source_url.startswith(("http://", "https://")):
        href = html.escape(claim.source_url, quote=True)
        link_html = (
            f'<a class="ci-link" href="{href}" target="_blank" '
            f'rel="noopener noreferrer">Source <span class="ci-chevron">›</span></a>'
        )
    return (
        f'<div class="ci-claim">{_e(claim.claim)}</div>'
        f"{quote_html}{link_html}"
    )


def render_comparison_matrix(
    company_order: list[str],
    rows,
    labels: dict[str, str] | None = None,
) -> str:
    labels = labels or competitor_labels()
    header_cells = "".join(
        f"<th>{_e(labels.get(cid, cid))}</th>" for cid in company_order
    )
    body_parts: list[str] = []
    mobile_parts: list[str] = []
    for row in rows:
        cells: list[str] = [f"<td>{_e(row.capability_label)}</td>"]
        claim_blocks: list[str] = []
        for cid, claim in zip(company_order, row.claims):
            cell = _claim_cell_html(claim)
            cells.append(f"<td>{cell}</td>")
            claim_blocks.append(
                f'<div class="ci-matrix-claim-block">'
                f'<div class="ci-matrix-company">{_e(labels.get(cid, cid))}</div>'
                f"{cell}</div>"
            )
        body_parts.append("<tr>" + "".join(cells) + "</tr>")
        mobile_parts.append(
            f'<article class="ci-matrix-card">'
            f'<h4 class="ci-matrix-card-title">{_e(row.capability_label)}</h4>'
            f'{"".join(claim_blocks)}</article>'
        )

    return f"""
    <div class="ci-matrix ci-desktop-table">
      <table>
        <thead><tr><th>Capability</th>{header_cells}</tr></thead>
        <tbody>{"".join(body_parts)}</tbody>
      </table>
    </div>
    <div class="ci-matrix-mobile" aria-label="Comparison cards">
      {"".join(mobile_parts)}
    </div>
    """


_ERR_PREVIEW_CHARS = 42


def _render_error_popup(raw: str | None, uid: str) -> tuple[str, str]:
    """Return (inline trigger HTML, modal HTML to place outside overflow parents)."""
    text = (str(raw).strip() if raw is not None else "")
    if not text:
        return ('<span class="ci-err-short">—</span>', "")
    full = _e(text)
    if len(text) <= _ERR_PREVIEW_CHARS:
        return (f'<span class="ci-err-short">{full}</span>', "")
    preview = _e(text[:_ERR_PREVIEW_CHARS].rstrip() + "…")
    trigger = (
        f'<label for="ci-err-{uid}" class="ci-err-trigger" title="Show full error">'
        f"{preview}</label>"
    )
    modal = (
        f'<input type="checkbox" id="ci-err-{uid}" class="ci-err-toggle" />'
        f'<div class="ci-err-modal" role="dialog" aria-modal="true">'
        f'<label for="ci-err-{uid}" class="ci-err-backdrop" aria-label="Close"></label>'
        f'<div class="ci-err-dialog">'
        f'<div class="ci-err-dialog-head">'
        f"<span>Error details</span>"
        f'<label for="ci-err-{uid}" class="ci-err-close" aria-label="Close">×</label>'
        f"</div>"
        f'<pre class="ci-err-body">{full}</pre>'
        f"</div></div>"
    )
    return trigger, modal


def _status_badge_html(status: str | None) -> str:
    raw = str(status or "")
    cls = "ci-run-status"
    if raw == "degraded":
        cls += " ci-run-status-degraded"
    elif raw == "failed":
        cls += " ci-run-status-failed"
    elif raw == "success":
        cls += " ci-run-status-ok"
    return f'<span class="{cls}">{_e(raw)}</span>'


def render_run_history(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return '<p class="ci-muted">No pipeline runs recorded yet.</p>'
    rows_html: list[str] = []
    cards_html: list[str] = []
    modals_html: list[str] = []
    for i, r in enumerate(runs):
        started = format_israel_time(r.get("started_at"))
        trigger = _e(r.get("trigger"))
        status_html = _status_badge_html(r.get("status"))
        fetched = _e(r.get("items_fetched"))
        new = _e(r.get("items_new"))
        scored = _e(r.get("items_scored"))
        ok = _e(r.get("items_classified_ok") if r.get("items_classified_ok") is not None else "—")
        fallback = _e(r.get("items_fallback") if r.get("items_fallback") is not None else "—")
        retries = _e(r.get("retries_used") if r.get("retries_used") is not None else "—")
        uid = _e(r.get("id") if r.get("id") is not None else i)
        err_d_trigger, err_d_modal = _render_error_popup(r.get("error_message"), f"d-{uid}")
        err_m_trigger, err_m_modal = _render_error_popup(r.get("error_message"), f"m-{uid}")
        if err_d_modal:
            modals_html.append(err_d_modal)
        if err_m_modal:
            modals_html.append(err_m_modal)
        rows_html.append(
            "<tr>"
            f"<td>{_e(started)}</td>"
            f"<td>{trigger}</td>"
            f"<td>{status_html}</td>"
            f"<td>{fetched}</td>"
            f"<td>{new}</td>"
            f"<td>{ok}</td>"
            f"<td>{fallback}</td>"
            f"<td>{retries}</td>"
            f"<td>{scored}</td>"
            f'<td class="ci-run-err">{err_d_trigger}</td>'
            "</tr>"
        )
        cards_html.append(
            f'<article class="ci-run-card">'
            f'<div class="ci-run-card-top">'
            f"{status_html}"
            f'<span class="ci-muted">{_e(started)}</span>'
            f"</div>"
            f'<div class="ci-run-card-meta">Trigger: {trigger}</div>'
            f'<div class="ci-run-card-stats">'
            f"<span>Fetched <strong>{fetched}</strong></span>"
            f"<span>New <strong>{new}</strong></span>"
            f"<span>OK <strong>{ok}</strong></span>"
            f"<span>Fallback <strong>{fallback}</strong></span>"
            f"<span>Retries <strong>{retries}</strong></span>"
            f"</div>"
            f'<div class="ci-run-card-err">Error: {err_m_trigger}</div>'
            f"</article>"
        )
    return f"""
    <div class="ci-run-table ci-desktop-table">
      <table>
        <thead>
          <tr>
            <th>Started (Israel)</th><th>Trigger</th><th>Status</th>
            <th>Fetched</th><th>New</th><th>OK</th><th>Fallback</th>
            <th>Retries</th><th>Scored</th><th>Error</th>
          </tr>
        </thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </div>
    <div class="ci-run-mobile" aria-label="Pipeline run cards">
      {"".join(cards_html)}
    </div>
    <div class="ci-err-modals">{"".join(modals_html)}</div>
    """
