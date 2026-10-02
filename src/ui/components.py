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


def render_score_ring(score: float | None) -> str:
    if score is None:
        pct = 0
        label = "—"
    else:
        pct = max(0, min(100, int(round((float(score) / SCORE_MAX) * 100))))
        label = f"{float(score):.2f}"
    # Inline conic-gradient (no CSS vars) — Streamlit sanitizers often strip `--pct`.
    ring_bg = (
        f"background:conic-gradient(#40BE46 {pct}%,#333642 0);"
    )
    return (
        f'<div class="ci-score-wrap">'
        f'<div class="ci-score-ring" style="{ring_bg}">'
        f'<div class="ci-score-ring-inner">{_e(label)}</div>'
        f"</div>"
        f'<div class="ci-score-label">relevance</div>'
        f"</div>"
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
    score = item.get("relevance_score")
    published = format_israel_time(
        item.get("published_at") or item.get("ingested_at")
    )

    dims = item.get("dimensions") or {}
    dim_html = ""
    if dims:
        chips = "".join(
            f'<span class="ci-dim-chip">{_e(k.replace("_", " "))}: {_e(v)}</span>'
            for k, v in dims.items()
        )
        dim_html = f'<div class="ci-dims">{chips}</div>'

    link_html = ""
    if safe_href:
        link_html = (
            f'<a class="ci-link" href="{safe_href}" target="_blank" rel="noopener noreferrer">'
            f"Source <span class=\"ci-chevron\">›</span></a>"
        )

    score_html = render_score_ring(float(score) if score is not None else None)
    # Single-line outer structure so Streamlit does not fragment the card.
    return (
        f'<div class="ci-card">'
        f'<div class="ci-card-score">{score_html}</div>'
        f'<div class="ci-card-main">'
        f"{render_competitor_badge(str(competitor))}"
        f"{render_category_badge(category)}"
        f'<h3 class="ci-card-title">{title}</h3>'
        f'<p class="ci-card-summary">{summary}</p>'
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


def render_run_history(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return '<p class="ci-muted">No pipeline runs recorded yet.</p>'
    rows_html: list[str] = []
    cards_html: list[str] = []
    for r in runs:
        started = format_israel_time(r.get("started_at"))
        trigger = _e(r.get("trigger"))
        status = _e(r.get("status"))
        fetched = _e(r.get("items_fetched"))
        new = _e(r.get("items_new"))
        scored = _e(r.get("items_scored"))
        err = _e(r.get("error_message") or "—")
        rows_html.append(
            "<tr>"
            f"<td>{_e(started)}</td>"
            f"<td>{trigger}</td>"
            f"<td>{status}</td>"
            f"<td>{fetched}</td>"
            f"<td>{new}</td>"
            f"<td>{scored}</td>"
            f"<td>{err}</td>"
            "</tr>"
        )
        cards_html.append(
            f'<article class="ci-run-card">'
            f'<div class="ci-run-card-top">'
            f'<span class="ci-run-card-status">{status}</span>'
            f'<span class="ci-muted">{_e(started)}</span>'
            f"</div>"
            f'<div class="ci-run-card-meta">Trigger: {trigger}</div>'
            f'<div class="ci-run-card-stats">'
            f"<span>Fetched <strong>{fetched}</strong></span>"
            f"<span>New <strong>{new}</strong></span>"
            f"<span>Scored <strong>{scored}</strong></span>"
            f"</div>"
            f'<div class="ci-muted">Error: {err}</div>'
            f"</article>"
        )
    return f"""
    <div class="ci-run-table ci-desktop-table">
      <table>
        <thead>
          <tr>
            <th>Started (Israel)</th><th>Trigger</th><th>Status</th>
            <th>Fetched</th><th>New</th><th>Scored</th><th>Error</th>
          </tr>
        </thead>
        <tbody>{"".join(rows_html)}</tbody>
      </table>
    </div>
    <div class="ci-run-mobile" aria-label="Pipeline run cards">
      {"".join(cards_html)}
    </div>
    """
