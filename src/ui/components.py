"""Safe HTML helpers for the Streamlit UI (all dynamic text is escaped)."""

from __future__ import annotations

import html
from typing import Any

from src.config_loader import load_competitors

# Max score on the LLM dimension scale (weighted total typically ~1–5).
SCORE_MAX = 5.0


def _e(value: Any) -> str:
    """Escape untrusted / LLM / user text for unsafe_allow_html rendering."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


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
    glow_cls = " glow" if glow else ""
    return f"""
    <div class="ci-kpi">
      <div class="ci-kpi-value{glow_cls}">{_e(value)}</div>
      <div class="ci-kpi-label">{_e(label)}</div>
    </div>
    """


def render_kpi_row(kpis: list[tuple[str, str, bool]]) -> str:
    cells = "".join(render_kpi(v, lab, glow=g) for v, lab, g in kpis)
    return f'<div class="ci-kpi-row">{cells}</div>'


def render_score_ring(score: float | None) -> str:
    if score is None:
        pct = 0
        label = "—"
    else:
        pct = max(0, min(100, int(round((float(score) / SCORE_MAX) * 100))))
        label = f"{float(score):.2f}"
    return f"""
    <div class="ci-score-wrap">
      <div class="ci-score-ring" style="--pct: {pct};">
        <div class="ci-score-ring-inner">{_e(label)}</div>
      </div>
      <div class="ci-score-label">relevance</div>
    </div>
    """


def render_competitor_badge(competitor_id: str) -> str:
    colors = competitor_colors()
    labels = competitor_labels()
    color = colors.get(competitor_id, "#333642")
    label = labels.get(competitor_id, competitor_id.replace("_", " ").title())
    # industry is not in competitors.yaml — soft gray tile.
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
    """Render a digest news card. Dynamic fields are HTML-escaped."""
    title = _e(item.get("title") or "Untitled")
    summary = _e(item.get("summary") or "")
    url = item.get("url") or ""
    # Only allow http(s) links in href
    safe_href = ""
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        safe_href = html.escape(url, quote=True)
    competitor = item.get("competitor") or "industry"
    category = item.get("category")
    score = item.get("relevance_score")
    published = _e(item.get("published_at") or item.get("ingested_at") or "")

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

    return f"""
    <div class="ci-card">
      <div class="ci-card-header">
        <div>
          {render_competitor_badge(str(competitor))}
          {render_category_badge(category)}
          <h3 class="ci-card-title">{title}</h3>
          <p class="ci-card-summary">{summary}</p>
          {link_html}
          <div class="ci-muted" style="margin-top:0.4rem;">{published}</div>
          {dim_html}
        </div>
        {render_score_ring(float(score) if score is not None else None)}
      </div>
    </div>
    """


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
    for row in rows:
        cells: list[str] = [f"<td>{_e(row.capability_label)}</td>"]
        for claim in row.claims:
            if claim.is_unknown:
                cells.append('<td><span class="ci-unknown">Unknown</span></td>')
                continue
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
            cells.append(
                f'<td><div class="ci-claim">{_e(claim.claim)}</div>'
                f"{quote_html}{link_html}</td>"
            )
        body_parts.append("<tr>" + "".join(cells) + "</tr>")

    return f"""
    <table class="ci-matrix">
      <thead><tr><th>Capability</th>{header_cells}</tr></thead>
      <tbody>{"".join(body_parts)}</tbody>
    </table>
    """


def render_run_history(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return '<p class="ci-muted">No pipeline runs recorded yet.</p>'
    rows_html = []
    for r in runs:
        rows_html.append(
            "<tr>"
            f"<td>{_e(r.get('started_at'))}</td>"
            f"<td>{_e(r.get('trigger'))}</td>"
            f"<td>{_e(r.get('status'))}</td>"
            f"<td>{_e(r.get('items_fetched'))}</td>"
            f"<td>{_e(r.get('items_new'))}</td>"
            f"<td>{_e(r.get('items_scored'))}</td>"
            f"<td>{_e(r.get('error_message') or '—')}</td>"
            "</tr>"
        )
    return f"""
    <table class="ci-run-table">
      <thead>
        <tr>
          <th>Started</th><th>Trigger</th><th>Status</th>
          <th>Fetched</th><th>New</th><th>Scored</th><th>Error</th>
        </tr>
      </thead>
      <tbody>{"".join(rows_html)}</tbody>
    </table>
    """
