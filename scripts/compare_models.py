#!/usr/bin/env python3
"""A/B compare OLD vs NEW scoring rubric on fixed stored titles (no DB writes).

Primary model only. Batches of 5 → about 6 live generate_content calls
(3 batches × 2 rubric versions) for 13 items.

Groups:
  - targets (3): items previously over-scored / under discussion
  - low (5): should stay LOW under the new rubric
  - high (5): should stay HIGH under the new rubric

Usage (from repo root, when quota allows):
  PYTHONPATH=. python scripts/compare_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_model_config, load_weights
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

# Fixed sample — match by title substring (case-insensitive).
TARGET_TITLES = [
    "The Open-Source Artifactory Alternative",
    "Evo ADS Govern Agent Behavior Goes GA",
    "Trust Docker for the agents you don't",
]
LOW_TITLES = [
    "What's new in Git 2.56.0?",
    "What is Interactive Application Security Testing (IAST)?",
    "AI is changing developer work. Here are three skills to strengthen.",
    "Selected models in GitHub Copilot deprecated",
    "GPT-6 Sol on Codex: average scores, quarter the cost",
]
HIGH_TITLES = [
    "Sckit Supply Chain Worm Hits MemTensor npm & PyPi scopes",
    "GitLab and Claude Code: Fast, compliant AI",
    "Securing the Financial Frontier: How Capital One Uses Socket for Open Source Security",
    "Pretty Themes, Hidden Loaders: GlassWorm-Linked Extensions",
    "Agents Pick Dependencies, DoWI 8430.01 Holds You Accountable",
]


def _norm(title: str) -> str:
    return " ".join((title or "").lower().split())


def _find_row(rows: list[dict], needle: str) -> dict | None:
    n = _norm(needle)
    for row in rows:
        if n in _norm(str(row.get("title") or "")):
            return row
    return None


def _pick_group(rows: list[dict], needles: list[str], label: str) -> list[dict]:
    picked: list[dict] = []
    missing: list[str] = []
    for needle in needles:
        row = _find_row(rows, needle)
        if row is None:
            missing.append(needle)
        else:
            picked.append(row)
    if missing:
        print(f"WARNING: {label} missing {len(missing)} title(s):", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
    return picked


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


def _score_entries(
    entries: list[NormalizedEntry],
    *,
    model_id: str,
    cfg: dict,
    weights: dict[str, float],
    legacy_rubric: bool,
) -> dict[str, float]:
    """Classify in batches of 5. Returns content_hash -> weighted total. No DB writes."""
    configure_llm_interval(model_min_interval_seconds(cfg, model_id))
    out: dict[str, float] = {}
    for batch in chunk_entries(entries, 5):
        ids = [batch_item_id(e) for e in batch]
        accepted, missing, _retries = classify_entries_batch(
            batch,
            model_config=cfg,
            item_ids=ids,
            model_id_override=model_id,
            usage_guard=None,  # throwaway - do not touch llm_usage soft counters
            legacy_rubric=legacy_rubric,
        )
        if missing:
            tag = "OLD" if legacy_rubric else "NEW"
            print(
                f"WARNING: {tag} rubric missing ids after batch: {missing}",
                file=sys.stderr,
            )
        for item_id, result in accepted.items():
            out[item_id] = weighted_score(result.dimension_dict(), weights)
    return out


def _print_group_table(
    label: str,
    rows: list[dict],
    scores_old: dict[str, float],
    scores_new: dict[str, float],
) -> tuple[float, float, int, int]:
    """Print one table; return (mean_old, mean_new, n_ge3_old, n_ge3_new)."""
    print(f"=== {label} ({len(rows)} items) ===")
    print(f"{'title':<56} {'old':>6} {'new':>6} {'diff':>7}")
    print("-" * 78)
    olds: list[float] = []
    news: list[float] = []
    for row in rows:
        entry = _to_entry(row)
        iid = batch_item_id(entry)
        title = (row.get("title") or "")[:54]
        old = scores_old.get(iid)
        new = scores_new.get(iid)
        if old is None or new is None:
            print(f"{title:<56} {'?':>6} {'?':>6} {'n/a':>7}")
            continue
        olds.append(old)
        news.append(new)
        print(f"{title:<56} {old:6.2f} {new:6.2f} {new - old:+7.2f}")
    mean_old = sum(olds) / len(olds) if olds else 0.0
    mean_new = sum(news) / len(news) if news else 0.0
    ge3_old = sum(1 for v in olds if v >= 3.0)
    ge3_new = sum(1 for v in news if v >= 3.0)
    print(
        f"mean old={mean_old:.3f}  mean new={mean_new:.3f}  "
        f">=3.0 old={ge3_old}/{len(olds)}  new={ge3_new}/{len(news)}"
    )
    print()
    return mean_old, mean_new, ge3_old, ge3_new


def main() -> int:
    if not turso_configured():
        print("Turso is not configured; need stored items to compare.", file=sys.stderr)
        return 2
    init_db()
    conn = get_connection()
    try:
        repo = Repository(conn)
        rows = repo.list_news_with_scores()

        targets = _pick_group(rows, TARGET_TITLES, "targets")
        lows = _pick_group(rows, LOW_TITLES, "low")
        highs = _pick_group(rows, HIGH_TITLES, "high")
        groups = [
            ("targets", targets),
            ("low (should stay LOW)", lows),
            ("high (should stay HIGH)", highs),
        ]
        all_rows = targets + lows + highs
        if len(all_rows) != 13:
            print(
                f"Need all 13 fixed titles in storage; found {len(all_rows)}. Aborting.",
                file=sys.stderr,
            )
            return 2

        cfg = load_model_config()
        weights = load_weights()
        model_id = resolve_pipeline_model(cfg, use_fallback=False)
        entries = [_to_entry(r) for r in all_rows]
        n_batches = len(chunk_entries(entries, 5))
        live_calls = n_batches * 2

        print(f"Rubric A/B on {len(entries)} stored items (primary model only)")
        print(f"  model: {model_id}")
        print(f"  rubric OLD = SCORING_CALIBRATION_LEGACY")
        print(
            f"  rubric NEW = SCORING_CALIBRATION_CURRENT "
            f"(config rubric_version={cfg.get('rubric_version')!r})"
        )
        print(f"  batches of 5 × 2 rubrics ≈ {live_calls} live calls")
        print(f"  in-memory only — no DB writes")
        print()

        try:
            scores_old = _score_entries(
                entries,
                model_id=model_id,
                cfg=cfg,
                weights=weights,
                legacy_rubric=True,
            )
        except DailyQuotaError as exc:
            print(
                f"STOPPED: {model_id} hit daily PerDay quota during OLD rubric "
                f"(hint={exc.retry_hint!r}).",
                file=sys.stderr,
            )
            return 3
        try:
            scores_new = _score_entries(
                entries,
                model_id=model_id,
                cfg=cfg,
                weights=weights,
                legacy_rubric=False,
            )
        except DailyQuotaError as exc:
            print(
                f"STOPPED: {model_id} hit daily PerDay quota during NEW rubric "
                f"(hint={exc.retry_hint!r}).",
                file=sys.stderr,
            )
            return 3

        summary_rows: list[tuple[str, float, float, int, int, int]] = []
        for label, group_rows in groups:
            mean_old, mean_new, ge3_old, ge3_new = _print_group_table(
                label, group_rows, scores_old, scores_new
            )
            summary_rows.append(
                (label, mean_old, mean_new, ge3_old, ge3_new, len(group_rows))
            )

        print("=== group summary ===")
        print(
            f"{'group':<28} {'mean_old':>9} {'mean_new':>9} "
            f"{'>=3 old':>8} {'>=3 new':>8}"
        )
        print("-" * 68)
        for label, mean_old, mean_new, ge3_old, ge3_new, n in summary_rows:
            print(
                f"{label:<28} {mean_old:9.3f} {mean_new:9.3f} "
                f"{ge3_old:>3}/{n:<3} {ge3_new:>3}/{n:<3}"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
