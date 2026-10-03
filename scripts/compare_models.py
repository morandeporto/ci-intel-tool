#!/usr/bin/env python3
"""Throwaway side-by-side score comparison of two Gemini models (no DB writes).

Exactly four live generate_content calls:
  - 2 batches of 5 on pipeline_model (gemini-3.8-flash)
  - 2 batches of 5 on fallback_model (gemini-3.5-flash-lite)

Usage (from repo root):
  PYTHONPATH=. python scripts/compare_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_model_config, load_sources, load_weights
from src.db.connection import get_connection, init_db, turso_configured
from src.db.repository import Repository
from src.ingest.normalize import NormalizedEntry
from src.process.llm_classify import (
    batch_item_id,
    chunk_entries,
    classify_entries_batch,
)
from src.process.llm_quota import (
    DailyQuotaError,
    model_min_interval_seconds,
    resolve_pipeline_model,
)
from src.process.llm_rate_limit import configure_llm_interval
from src.process.scoring import weighted_score


def _pick_ten(rows: list[dict], source_meta: dict[str, dict]) -> list[dict]:
    """Prefer a mix of official_competitor / emerging / industry (+community as industry)."""
    buckets: dict[str, list[dict]] = {
        "official_competitor": [],
        "emerging": [],
        "industry": [],
    }
    for row in rows:
        if row.get("is_fallback") or row.get("relevance_score") is None:
            continue
        if str(row.get("status") or "") in ("pending_scoring", "scoring", "filtered"):
            continue
        sid = str(row.get("source_id") or "")
        kind = str((source_meta.get(sid) or {}).get("kind") or "")
        if kind == "community":
            kind = "industry"
        if kind not in buckets:
            continue
        buckets[kind].append(row)

    picked: list[dict] = []
    # Round-robin to mix kinds: aim ~4 / 3 / 3.
    targets = [("official_competitor", 4), ("emerging", 3), ("industry", 3)]
    for kind, n in targets:
        picked.extend(buckets[kind][:n])
    # Top up from any remaining if a bucket was short.
    if len(picked) < 10:
        seen = {r["id"] for r in picked}
        for kind in ("official_competitor", "emerging", "industry"):
            for row in buckets[kind]:
                if row["id"] in seen:
                    continue
                picked.append(row)
                seen.add(row["id"])
                if len(picked) >= 10:
                    break
            if len(picked) >= 10:
                break
    return picked[:10]


def _to_entry(row: dict) -> NormalizedEntry:
    return NormalizedEntry(
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        published_at=row.get("published_at"),
        raw_excerpt=str(row.get("raw_excerpt") or ""),
        source_id=str(row.get("source_id") or ""),
        competitor=str(row.get("competitor") or "industry"),
        content_hash=str(row.get("content_hash") or row.get("id") or ""),
    )


def _score_ten(
    entries: list[NormalizedEntry],
    *,
    model_id: str,
    cfg: dict,
    weights: dict[str, float],
) -> dict[str, float]:
    """Classify in two batches of 5. Returns content_hash -> total score."""
    configure_llm_interval(model_min_interval_seconds(cfg, model_id))
    out: dict[str, float] = {}
    batches = chunk_entries(entries, 5)
    if len(batches) != 2:
        raise SystemExit(f"Expected exactly 2 batches of 5, got {len(batches)}")
    for batch in batches:
        ids = [batch_item_id(e) for e in batch]
        accepted, missing, _retries = classify_entries_batch(
            batch,
            model_config=cfg,
            item_ids=ids,
            model_id_override=model_id,
            usage_guard=None,  # throwaway script — do not touch llm_usage soft counters
        )
        if missing:
            print(f"WARNING: {model_id} missing ids after batch: {missing}", file=sys.stderr)
        for item_id, result in accepted.items():
            out[item_id] = weighted_score(result.dimension_dict(), weights)
    return out


def main() -> int:
    if not turso_configured():
        print("Turso is not configured; need stored items to compare.", file=sys.stderr)
        return 2
    init_db()
    conn = get_connection()
    try:
        repo = Repository(conn)
        rows = repo.list_news_with_scores()
        source_meta = {str(s["id"]): s for s in load_sources()}
        picked = _pick_ten(rows, source_meta)
        if len(picked) < 10:
            print(f"Need 10 scored items with mixed kinds; found {len(picked)}", file=sys.stderr)
            return 2

        cfg = load_model_config()
        weights = load_weights()
        model_a = resolve_pipeline_model(cfg, use_fallback=False)
        model_b = resolve_pipeline_model(cfg, use_fallback=True)
        entries = [_to_entry(r) for r in picked]
        id_order = [batch_item_id(e) for e in entries]
        meta_by_id = {batch_item_id(_to_entry(r)): r for r in picked}

        print(f"Comparing {len(entries)} items")
        print(f"  model A (pipeline): {model_a}  — 2 live batch calls")
        print(f"  model B (fallback): {model_b}  — 2 live batch calls")
        print()

        try:
            scores_a = _score_ten(entries, model_id=model_a, cfg=cfg, weights=weights)
        except DailyQuotaError as exc:
            print(
                f"STOPPED: model A ({model_a}) hit daily PerDay quota "
                f"(hint={exc.retry_hint!r}). Not calling model B to preserve quota.",
                file=sys.stderr,
            )
            return 3
        try:
            scores_b = _score_ten(entries, model_id=model_b, cfg=cfg, weights=weights)
        except DailyQuotaError as exc:
            print(
                f"STOPPED: model B ({model_b}) hit daily PerDay quota "
                f"(hint={exc.retry_hint!r}).",
                file=sys.stderr,
            )
            return 3

        print(
            f"{'title':<42} {'source':<18} {'A':>5} {'B':>5} {'diff':>6}"
        )
        print("-" * 80)
        diffs: list[float] = []
        for iid in id_order:
            row = meta_by_id[iid]
            title = (row.get("title") or "")[:40]
            source = str(row.get("source_id") or "")[:16]
            a = scores_a.get(iid)
            b = scores_b.get(iid)
            if a is None or b is None:
                print(f"{title:<42} {source:<18} {'?':>5} {'?':>5} {'n/a':>6}")
                continue
            diff = a - b
            diffs.append(abs(diff))
            print(f"{title:<42} {source:<18} {a:5.2f} {b:5.2f} {diff:+6.2f}")

        vals_a = [scores_a[i] for i in id_order if i in scores_a]
        vals_b = [scores_b[i] for i in id_order if i in scores_b]
        mean_a = sum(vals_a) / len(vals_a) if vals_a else 0.0
        mean_b = sum(vals_b) / len(vals_b) if vals_b else 0.0
        mad = sum(diffs) / len(diffs) if diffs else 0.0

        top_a = {
            i
            for i, _ in sorted(
                ((i, scores_a[i]) for i in scores_a), key=lambda x: -x[1]
            )[:3]
        }
        top_b = {
            i
            for i, _ in sorted(
                ((i, scores_b[i]) for i in scores_b), key=lambda x: -x[1]
            )[:3]
        }
        overlap = len(top_a & top_b)

        print()
        print(f"mean total A ({model_a}): {mean_a:.3f}")
        print(f"mean total B ({model_b}): {mean_b:.3f}")
        print(f"mean absolute difference |A-B|: {mad:.3f}")
        print(f"top-3 overlap: {overlap} / 3")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
