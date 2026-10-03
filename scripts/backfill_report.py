#!/usr/bin/env python3
"""Print a full post-backfill coverage/scoring report (no truncation)."""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_competitors, load_model_config, load_sources
from src.db.connection import db_label, get_connection, init_db, open_repo_connection
from src.db.models import DIMENSION_NAMES
from src.db.repository import Repository
from src.ingest.rss_fetcher import fetch_and_normalize
from src.process.dedupe import is_duplicate
from src.process.freshness import filter_by_freshness
from src.process.relevance_gate import evaluate_gate
from src.process.selection import select_for_llm
from src.config_loader import load_relevance_config


def _out(lines: list[str], text: str = "") -> None:
    lines.append(text)


def main() -> int:
    lines: list[str] = []
    model = load_model_config()
    sources = load_sources()
    competitors = {c["id"]: c for c in load_competitors()}
    relevance = load_relevance_config()
    window_hours = 7 * 24
    max_per_source = int(model["max_per_source"])
    max_items = int(model["max_items_per_run"])
    reserved = dict(model["selection"]["reserved_slots"])

    _out(lines, f"DB: {db_label()}")
    _out(lines, f"Generated (UTC): {datetime.now(timezone.utc).isoformat()}")
    _out(lines, "")

    # --- Live 7-day fetch simulation for verification + selection stats ---
    _out(lines, "== A. Sources (config + live 7-day fetch) ==")
    fetch = fetch_and_normalize(
        timeout=float(model.get("fetch_timeout_seconds", 15)),
        window_hours=window_hours,
    )
    fetched_by = Counter(e.source_id for e in fetch.entries)
    err_by = {e.source_id: e.message for e in fetch.errors}
    kind_enabled = Counter()

    _out(
        lines,
        f"{'id':28} {'kind':18} {'comp':12} {'en':3} {'verify':8} {'7d':5} note/reason",
    )
    for s in sources:
        sid = s["id"]
        kind = s.get("kind")
        comp = s.get("competitor") or "null"
        en = "Y" if s.get("enabled") else "N"
        if s.get("enabled"):
            kind_enabled[str(kind)] += 1
        if not s.get("enabled"):
            verify = "disabled"
            reason = (s.get("note") or "")[:80]
            count7 = "-"
        elif sid in err_by:
            verify = "FAIL"
            reason = err_by[sid][:80]
            count7 = "0"
        else:
            verify = "PASS"
            reason = (s.get("note") or "")[:60]
            count7 = str(fetched_by.get(sid, 0))
        _out(
            lines,
            f"{sid:28} {str(kind):18} {str(comp):12} {en:3} {verify:8} {count7:5} {reason}",
        )

    _out(lines, "")
    _out(lines, "Enabled sources per kind:")
    for k, n in sorted(kind_enabled.items()):
        _out(lines, f"  {k}: {n}")

    # Selection simulation (empty dedupe sets → all in-window compete)
    meta = {str(s["id"]): s for s in sources}
    in_window = filter_by_freshness(fetch.entries, window_hours=window_hours).kept
    passed = []
    filtered = []
    for entry in in_window:
        gate = (meta.get(entry.source_id) or {}).get("gate", "off")
        if gate is False:
            gate = "off"
        d = evaluate_gate(entry, gate=str(gate), relevance_cfg=relevance)
        if d.passed:
            passed.append(entry)
        else:
            filtered.append((entry, d.reason or "filtered"))
    selection = select_for_llm(
        passed,
        source_meta=meta,
        max_per_source=max_per_source,
        max_items=max_items,
        reserved_slots=reserved,
        relevance_cfg=relevance,
    )

    _out(lines, "")
    _out(lines, "== B. Selection simulation (7-day window, empty dedupe, daily cap) ==")
    _out(lines, f"fetched_in_window={len(fetch.entries)} passed_gate={len(passed)} "
         f"selected={len(selection.selected)} filtered={len(filtered)} "
         f"cap_skipped={len(selection.cap_skipped)}")
    _out(lines, f"Estimated Gemini calls per daily run (at max_items_per_run={max_items}): "
         f"{min(len(selection.selected), max_items)}")

    def bucket_rows(entries, label_fn):
        c = Counter(label_fn(e) for e in entries)
        return c

    _out(lines, "")
    _out(lines, "Per source: selected / filtered / cap_skipped (simulation)")
    all_sids = sorted(
        set(fetched_by)
        | {e.source_id for e, _ in filtered}
        | {e.source_id for e in selection.selected}
        | {e.source_id for e in selection.cap_skipped}
    )
    sel_c = Counter(e.source_id for e in selection.selected)
    fil_c = Counter(e.source_id for e, _ in filtered)
    skip_c = Counter(e.source_id for e in selection.cap_skipped)
    for sid in all_sids:
        _out(
            lines,
            f"  {sid:28} selected={sel_c[sid]:3} filtered={fil_c[sid]:3} "
            f"cap_skipped={skip_c[sid]:3}",
        )

    _out(lines, "")
    _out(lines, "Per competitor tag:")
    for label, counter in [
        ("selected", Counter(e.competitor for e in selection.selected)),
        ("filtered", Counter(e.competitor for e, _ in filtered)),
        ("cap_skipped", Counter(e.competitor for e in selection.cap_skipped)),
    ]:
        _out(lines, f"  {label}: {dict(counter)}")

    _out(lines, "")
    _out(lines, "== C. Filtered titles (industry + community sources) ==")
    for entry, reason in filtered:
        kind = (meta.get(entry.source_id) or {}).get("kind")
        if kind in ("industry", "community"):
            _out(lines, f"  [{entry.source_id}|{reason}] {entry.title}")

    # --- DB stats after backfill ---
    init_db()
    conn, _ = open_repo_connection()
    try:
        repo = Repository(conn)
        rows = repo.conn.execute(
            """
            SELECT id, title, url, source_id, competitor, published_at, status,
                   filter_reason, item_type, jfrog_implication, relevance_score,
                   is_fallback,
                   jfrog_relevance, competitor_signal, strategic_impact,
                   freshness, market_visibility
            FROM news_items n
            LEFT JOIN dimension_scores d ON d.news_item_id = n.id
            """
        )
        from src.db.repository import _rows_as_dicts

        cur = repo.conn.execute(
            """
            SELECT n.id, n.title, n.url, n.source_id, n.competitor, n.published_at,
                   n.status, n.filter_reason, n.item_type, n.jfrog_implication,
                   n.relevance_score, n.is_fallback,
                   d.jfrog_relevance, d.competitor_signal, d.strategic_impact,
                   d.freshness, d.market_visibility
            FROM news_items n
            LEFT JOIN dimension_scores d ON d.news_item_id = n.id
            """
        )
        db_rows = _rows_as_dicts(cur)
    finally:
        conn.close()

    classified = [
        r
        for r in db_rows
        if (r.get("status") or "classified") != "filtered" and not r.get("is_fallback")
        and r.get("relevance_score") is not None
    ]
    fallbacks = [r for r in db_rows if r.get("is_fallback")]
    filtered_db = [r for r in db_rows if (r.get("status") or "") == "filtered"]

    _out(lines, "")
    _out(lines, "== D. DB inventory ==")
    _out(lines, f"total_rows={len(db_rows)} classified_scored={len(classified)} "
         f"filtered={len(filtered_db)} fallback={len(fallbacks)}")

    by_type = Counter((r.get("item_type") or "null") for r in classified)
    _out(lines, f"classified by item_type: {dict(by_type)}")

    _out(lines, "")
    _out(lines, "== E. Score histogram (buckets of 0.5) ==")
    hist = Counter()
    for r in classified:
        score = float(r["relevance_score"])
        bucket = int(score // 0.5) * 0.5
        hist[bucket] += 1
    for b in sorted(hist):
        _out(lines, f"  [{b:.1f}, {b + 0.5:.1f}): {hist[b]}")

    _out(lines, "")
    _out(lines, "== F. Mean score per dimension (classified non-fallback) ==")
    for dim in DIMENSION_NAMES:
        vals = [int(r[dim]) for r in classified if r.get(dim) is not None]
        mean = (sum(vals) / len(vals)) if vals else float("nan")
        _out(lines, f"  {dim}: mean={mean:.3f} n={len(vals)}")

    scored_sorted = sorted(classified, key=lambda r: float(r["relevance_score"]), reverse=True)
    _out(lines, "")
    _out(lines, "== G. Top 20 classified by total score ==")
    for r in scored_sorted[:20]:
        _out(
            lines,
            f"  {float(r['relevance_score']):.2f} | {r.get('item_type')} | "
            f"{r.get('source_id')} | {r.get('title')}",
        )
        _out(lines, f"       implication: {r.get('jfrog_implication')}")

    _out(lines, "")
    _out(lines, "== H. Bottom 20 classified by total score ==")
    for r in list(reversed(scored_sorted[-20:])):
        _out(
            lines,
            f"  {float(r['relevance_score']):.2f} | {r.get('item_type')} | "
            f"{r.get('source_id')} | {r.get('title')}",
        )
        _out(lines, f"       implication: {r.get('jfrog_implication')}")

    _out(lines, "")
    _out(lines, "== I. Fallback count ==")
    _out(lines, f"fallback_items={len(fallbacks)}")

    text = "\n".join(lines) + "\n"
    out_path = Path("/tmp/backfill_report.txt")
    out_path.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"\nWrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
