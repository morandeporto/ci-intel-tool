"""Daily competitive-intelligence pipeline orchestration.

Flow: init_db → start_run → fetch → dedupe → freshness → gate → select →
classify+score (selected only) → persist → finish_run.

Per-item LLM failures do not abort the run (status becomes partial). Cost is
guarded by max_items_per_run / selection caps from config/model.yaml.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from src.config_loader import (
    DEFAULT_DB_PATH,
    load_model_config,
    load_relevance_config,
    load_sources,
    load_weights,
)
from src.db.connection import get_connection, init_db, turso_configured
from src.db.models import DimensionScores, NewsItem
from src.db.repository import Repository
from src.ingest.normalize import NormalizedEntry
from src.ingest.rss_fetcher import SourceFetchStat, fetch_and_normalize
from src.process.dedupe import is_duplicate
from src.process.freshness import filter_by_freshness
from src.process.llm_classify import (
    ClassificationResult,
    LlmUsageGuard,
    chunk_entries,
    classify_entries_batch_with_fallback,
)
from src.process.llm_quota import (
    DailyQuotaError,
    model_min_interval_seconds,
    resolve_pipeline_model,
)
from src.process.llm_rate_limit import configure_llm_interval
from src.process.relevance_gate import evaluate_gate
from src.process.rescore import select_fallbacks_for_rescore
from src.process.scoring import weighted_score
from src.process.selection import select_for_llm

logger = logging.getLogger(__name__)

RunTrigger = Literal["manual", "cron"]
RunStatus = Literal["success", "partial", "failed", "degraded"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class PipelineResult:
    """Summary returned to CLI / callers (no secrets, no stack traces)."""

    status: RunStatus
    run_id: str | None
    items_fetched: int = 0
    items_new: int = 0
    items_scored: int = 0
    items_failed: int = 0
    items_filtered: int = 0
    items_fallback: int = 0
    items_classified_ok: int = 0
    retries_used: int = 0
    items_rescored_ok: int = 0
    items_rescored_fallback: int = 0
    source_errors: int = 0
    source_warnings: int = 0
    dry_run: bool = False
    message: str = ""
    new_entries: list[NormalizedEntry] = field(default_factory=list)


def _source_meta_by_id() -> dict[str, dict[str, Any]]:
    return {str(s["id"]): s for s in load_sources()}


def _filter_entries_to_process(
    repo: Repository,
    entries: list[NormalizedEntry],
) -> list[NormalizedEntry]:
    """New items plus any feed row that matches a URL missing scores (retry)."""
    existing_urls = repo.existing_urls()
    existing_hashes = repo.existing_content_hashes()
    retry_urls = repo.urls_missing_dimension_scores()
    to_process: list[NormalizedEntry] = []
    urls = set(existing_urls)
    hashes = set(existing_hashes)
    for entry in entries:
        if entry.url in retry_urls:
            to_process.append(entry)
            continue
        if is_duplicate(entry.url, entry.content_hash, urls, hashes):
            continue
        to_process.append(entry)
        urls.add(entry.url)
        hashes.add(entry.content_hash)
    return to_process


def _filter_new_entries_simple(
    entries: list[NormalizedEntry],
    existing_urls: set[str],
    existing_hashes: set[str],
) -> list[NormalizedEntry]:
    """Dedupe only (dry-run when DB is unavailable)."""
    new_items: list[NormalizedEntry] = []
    urls = set(existing_urls)
    hashes = set(existing_hashes)
    for entry in entries:
        if is_duplicate(entry.url, entry.content_hash, urls, hashes):
            continue
        new_items.append(entry)
        urls.add(entry.url)
        hashes.add(entry.content_hash)
    return new_items


def _apply_freshness_gate_select(
    new_entries: list[NormalizedEntry],
    *,
    model_cfg: dict[str, Any],
    relevance_cfg: dict[str, Any],
    source_meta: dict[str, dict[str, Any]],
    effective_limit: int,
) -> tuple[
    list[NormalizedEntry],
    list[tuple[NormalizedEntry, str]],
    list[NormalizedEntry],
    list[NormalizedEntry],
]:
    """Return (selected, filtered_with_reason, cap_skipped, in_window)."""
    window_hours = int(model_cfg["window_hours"])
    max_per_source = int(model_cfg["max_per_source"])
    reserved = dict(model_cfg["selection"]["reserved_slots"])

    fresh = filter_by_freshness(new_entries, window_hours=window_hours)
    in_window = fresh.kept

    passed: list[NormalizedEntry] = []
    filtered: list[tuple[NormalizedEntry, str]] = []
    for entry in in_window:
        meta = source_meta.get(entry.source_id) or {}
        gate = meta.get("gate", "off")
        if gate is False:
            gate = "off"
        decision = evaluate_gate(entry, gate=str(gate), relevance_cfg=relevance_cfg)
        if decision.passed:
            passed.append(entry)
        else:
            filtered.append((entry, decision.reason or "filtered"))

    selection = select_for_llm(
        passed,
        source_meta=source_meta,
        max_per_source=max_per_source,
        max_items=effective_limit,
        reserved_slots=reserved,
        relevance_cfg=relevance_cfg,
    )
    return selection.selected, filtered, selection.cap_skipped, in_window


def _count_by_source(entries: list[NormalizedEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.source_id] = counts.get(entry.source_id, 0) + 1
    return counts


def _build_source_run_stat_rows(
    *,
    run_id: str,
    fetch_stats: list[SourceFetchStat],
    new_entries: list[NormalizedEntry],
    in_window: list[NormalizedEntry],
    filtered: list[tuple[NormalizedEntry, str]],
    selected: list[NormalizedEntry],
    classified_by_source: dict[str, int],
) -> list[dict[str, Any]]:
    new_c = _count_by_source(new_entries)
    window_c = _count_by_source(in_window)
    filtered_urls = {e.url for e, _ in filtered}
    passed_entries = [e for e in in_window if e.url not in filtered_urls]
    passed_c = _count_by_source(passed_entries)
    selected_c = _count_by_source(selected)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stat in fetch_stats:
        sid = stat.source_id
        seen.add(sid)
        rows.append(
            {
                "run_id": run_id,
                "source_id": sid,
                "http_status": stat.http_status,
                "fetched": stat.fetched,
                "in_window": window_c.get(sid, 0),
                "new": new_c.get(sid, 0),
                "passed_gate": passed_c.get(sid, 0),
                "selected": selected_c.get(sid, 0),
                "classified": classified_by_source.get(sid, 0),
                "error": stat.error,
                "warning": getattr(stat, "warning", None),
                "duration_ms": stat.duration_ms,
            }
        )
    # Sources that only appear in new_entries (should be rare).
    for sid in sorted(set(new_c) | set(selected_c) - seen):
        rows.append(
            {
                "run_id": run_id,
                "source_id": sid,
                "http_status": None,
                "fetched": 0,
                "in_window": window_c.get(sid, 0),
                "new": new_c.get(sid, 0),
                "passed_gate": passed_c.get(sid, 0),
                "selected": selected_c.get(sid, 0),
                "classified": classified_by_source.get(sid, 0),
                "error": None,
                "warning": None,
                "duration_ms": None,
            }
        )
    return rows


def _persist_filtered(
    repo: Repository,
    entry: NormalizedEntry,
    reason: str,
    *,
    run_id: str,
) -> None:
    item = NewsItem(
        id=str(uuid4()),
        title=entry.title,
        url=entry.url,
        source_id=entry.source_id,
        competitor=entry.competitor,
        published_at=entry.published_at,
        ingested_at=_utc_now(),
        summary=None,
        category=None,
        raw_excerpt=entry.raw_excerpt,
        content_hash=entry.content_hash,
        relevance_score=None,
        run_id=run_id,
        status="filtered",
        filter_reason=reason,
    )
    repo.upsert_news_item(item)


def _row_to_normalized_entry(row: dict[str, Any]) -> NormalizedEntry:
    return NormalizedEntry(
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        published_at=row.get("published_at"),
        raw_excerpt=str(row.get("raw_excerpt") or ""),
        source_id=str(row.get("source_id") or ""),
        competitor=str(row.get("competitor") or "industry"),
        content_hash=str(row.get("content_hash") or ""),
    )


@dataclass
class RescoreStats:
    attempted: int = 0
    ok: int = 0
    still_fallback: int = 0
    retries_used: int = 0
    quota_stopped: bool = False
    errors: list[str] = field(default_factory=list)


def rescore_fallback_items(
    repo: Repository,
    *,
    model_cfg: dict[str, Any],
    weights: dict[str, float],
    source_meta: dict[str, dict[str, Any]],
    within_days: int | None = None,
    limit: int | None = None,
    model_id_override: str | None = None,
    usage_guard: LlmUsageGuard | None = None,
    use_fallback_model: bool = False,
) -> RescoreStats:
    """Re-classify stored is_fallback / pending_scoring rows (newest first, capped)."""
    days = int(
        within_days
        if within_days is not None
        else model_cfg.get("rescore_fallback_days", 3)
    )
    cap = int(
        limit if limit is not None else model_cfg.get("rescore_fallback_limit", 40)
    )
    timeout_min = int(model_cfg.get("scoring_claim_timeout_minutes", 30))
    repo.release_stale_scoring_claims(older_than_minutes=timeout_min)

    # Prefer pending_scoring + fallbacks (shared selection helper).
    from src.process.rescore import select_items_for_rescore

    candidates = select_items_for_rescore(
        repo.list_pending_or_fallback_items(),
        within_days=days,
        limit=cap,
    )
    stats = RescoreStats(attempted=0)
    if not candidates:
        return stats

    model_id = model_id_override or resolve_pipeline_model(
        model_cfg, use_fallback=use_fallback_model
    )
    configure_llm_interval(model_min_interval_seconds(model_cfg, model_id))
    guard = usage_guard or LlmUsageGuard(
        repo, model_cfg, purpose="rescore", model_id=model_id
    )
    batch_size = max(1, int(model_cfg.get("batch_size", 5)))
    source_kinds = {
        sid: str(meta.get("kind") or "") for sid, meta in source_meta.items()
    }

    # Claim atomically before classifying - skip rows another worker already took.
    claimed: list[dict[str, Any]] = []
    for row in candidates:
        news_id = str(row["id"])
        is_pending = str(row.get("status") or "") == "pending_scoring"
        ok = (
            repo.claim_item_for_scoring(news_id)
            if is_pending
            else repo.claim_fallback_item_for_scoring(news_id)
        )
        if ok:
            claimed.append(row)
    stats.attempted = len(claimed)
    if not claimed:
        return stats

    now = _utc_now()
    for batch_rows in [
        claimed[i : i + batch_size] for i in range(0, len(claimed), batch_size)
    ]:
        entries = [_row_to_normalized_entry(r) for r in batch_rows]
        item_ids = [str(r["id"]) for r in batch_rows]
        try:
            outcomes = classify_entries_batch_with_fallback(
                entries,
                model_config=model_cfg,
                source_kinds=source_kinds,
                item_ids=item_ids,
                usage_guard=guard,
                model_id_override=model_id,
            )
        except DailyQuotaError as exc:
            # Release claimed-but-unscored rows back to pending, do not fail the job.
            stats.quota_stopped = True
            for row in batch_rows:
                try:
                    repo.conn.execute(
                        """
                        UPDATE news_items
                        SET status = 'pending_scoring',
                            filter_reason = 'daily_quota',
                            is_fallback = 0
                        WHERE id = ? AND status = 'scoring'
                        """,
                        (row["id"],),
                    )
                    repo.conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            stats.errors.append(f"daily quota during rescore: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"rescore batch failed: {exc}")
            continue

        for row, (_entry, result, used_fallback, err, retries) in zip(
            batch_rows, outcomes, strict=True
        ):
            stats.retries_used += int(retries)
            news_id = str(row["id"])
            try:
                if used_fallback:
                    stats.still_fallback += 1
                    if err:
                        stats.errors.append(f"{row.get('url')}: {err}")
                    was_pending = str(row.get("status") or "") in (
                        "pending_scoring",
                        "scoring",
                    )
                    if was_pending:
                        # Keep awaiting a later night / run - not a mid-score fallback.
                        repo.conn.execute(
                            """
                            UPDATE news_items
                            SET status = 'pending_scoring',
                                filter_reason = ?,
                                is_fallback = 0
                            WHERE id = ?
                            """,
                            (err or "rescore_failed", news_id),
                        )
                        repo.conn.commit()
                    else:
                        # Original fallback row - leave is_fallback=1 placeholders.
                        repo.conn.execute(
                            """
                            UPDATE news_items
                            SET status = 'classified', filter_reason = NULL
                            WHERE id = ? AND status = 'scoring'
                            """,
                            (news_id,),
                        )
                        repo.conn.commit()
                    continue
                score = weighted_score(result.dimension_dict(), weights)
                dims = DimensionScores(
                    jfrog_relevance=result.jfrog_relevance,
                    competitor_signal=result.competitor_signal,
                    strategic_impact=result.strategic_impact,
                    freshness=result.freshness,
                    market_visibility=result.market_visibility,
                    model_id=model_id,
                    scored_at=now,
                )
                repo.apply_classification_to_item(
                    news_id,
                    summary=result.summary,
                    category=result.category,
                    item_type=result.item_type,
                    jfrog_implication=result.jfrog_implication,
                    relevance_score=score,
                    is_fallback=False,
                    scores=dims,
                    scored_by_model=model_id,
                )
                stats.ok += 1
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{row.get('url')}: rescore persist failed: {exc}")
    return stats


def _persist_pending_scoring(
    repo: Repository,
    entry: NormalizedEntry,
    *,
    run_id: str,
) -> None:
    """Store an item that could not be scored due to daily quota (not a fallback)."""
    news_id = str(uuid4())
    now = _utc_now()
    item = NewsItem(
        id=news_id,
        title=entry.title,
        url=entry.url,
        source_id=entry.source_id,
        competitor=entry.competitor,
        published_at=entry.published_at,
        ingested_at=now,
        summary=None,
        category=None,
        raw_excerpt=entry.raw_excerpt,
        content_hash=entry.content_hash,
        relevance_score=None,
        run_id=run_id,
        status="pending_scoring",
        filter_reason="daily_quota",
        item_type=None,
        jfrog_implication=None,
        is_fallback=False,
    )
    repo.upsert_news_item(item)


def _persist_classified(
    repo: Repository,
    entry: NormalizedEntry,
    result: ClassificationResult,
    *,
    weights: dict[str, float],
    model_id: str,
    run_id: str,
    is_fallback: bool = False,
) -> None:
    # Fallback rows keep mid dimension scores for debugging but no ranking total.
    score = None if is_fallback else weighted_score(result.dimension_dict(), weights)
    # Strip legacy ":fallback" suffix if a caller still passes it.
    scored_by = model_id.split(":", 1)[0] if model_id else None
    news_id = str(uuid4())
    now = _utc_now()
    item = NewsItem(
        id=news_id,
        title=entry.title,
        url=entry.url,
        source_id=entry.source_id,
        competitor=entry.competitor,
        published_at=entry.published_at,
        ingested_at=now,
        summary=result.summary,
        category=result.category,
        raw_excerpt=entry.raw_excerpt,
        content_hash=entry.content_hash,
        relevance_score=score,
        run_id=run_id,
        status="classified",
        item_type=result.item_type,
        jfrog_implication=result.jfrog_implication,
        is_fallback=is_fallback,
        scored_by_model=scored_by,
    )
    news_id = repo.upsert_news_item(item)
    if not is_fallback:
        dims = DimensionScores(
            jfrog_relevance=result.jfrog_relevance,
            competitor_signal=result.competitor_signal,
            strategic_impact=result.strategic_impact,
            freshness=result.freshness,
            market_visibility=result.market_visibility,
            model_id=scored_by or model_id,
            scored_at=now,
        )
        repo.save_dimension_scores(news_id, dims)


def run_rescore_only(
    *,
    db_path: Path | str | None = None,
    within_days: int | None = None,
    limit: int | None = None,
    use_fallback_model: bool = False,
    trigger: RunTrigger = "manual",
) -> PipelineResult:
    """CLI path: rescore stored fallbacks / pending_scoring without fetching feeds."""
    model_cfg = load_model_config()
    weights = load_weights()
    source_meta = _source_meta_by_id()
    use_turso = turso_configured() and db_path is None
    path = None if use_turso else (Path(db_path) if db_path else DEFAULT_DB_PATH)
    if use_turso:
        init_db()
    else:
        assert path is not None
        init_db(path)

    def _connect():
        return get_connection() if use_turso else get_connection(path)

    try:
        active_model = resolve_pipeline_model(model_cfg, use_fallback=use_fallback_model)
    except ValueError as exc:
        return PipelineResult(status="failed", run_id=None, message=str(exc))

    conn = _connect()
    try:
        repo = Repository(conn)
        run_id = repo.start_run(trigger)
        stats = rescore_fallback_items(
            repo,
            model_cfg=model_cfg,
            weights=weights,
            source_meta=source_meta,
            within_days=within_days,
            limit=limit,
            model_id_override=active_model,
            use_fallback_model=use_fallback_model,
        )
        if stats.quota_stopped:
            status: RunStatus = "degraded"
        else:
            status = _resolve_status(
                items_fetched=0,
                items_new=0,
                items_scored=stats.ok,
                items_failed=len(stats.errors),
                items_fallback=stats.still_fallback,
                source_errors=0,
                attempted=stats.attempted,
            )
        msg = (
            f"Rescored pending/fallbacks: attempted={stats.attempted} ok={stats.ok} "
            f"still_fallback={stats.still_fallback} retries={stats.retries_used}"
        )
        if stats.quota_stopped:
            msg += " | daily quota - remaining items stay pending_scoring"
        repo.finish_run(
            run_id,
            status=status,
            items_fetched=0,
            items_new=0,
            items_scored=stats.ok,
            items_classified_ok=stats.ok,
            items_fallback=stats.still_fallback,
            retries_used=stats.retries_used,
            error_message=msg if status != "success" else None,
        )
        return PipelineResult(
            status=status,
            run_id=run_id,
            items_scored=stats.ok,
            items_classified_ok=stats.ok,
            items_fallback=stats.still_fallback,
            retries_used=stats.retries_used,
            items_rescored_ok=stats.ok,
            items_rescored_fallback=stats.still_fallback,
            message=msg,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_daily(
    *,
    trigger: RunTrigger = "manual",
    db_path: Path | str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    window_hours: int | None = None,
    skip_auto_rescore: bool = False,
    use_fallback_model: bool = False,
) -> PipelineResult:
    """Execute one daily ingestion + classification cycle.

    dry_run: fetch + dedupe + freshness + gate + select + print - no LLM, no writes.
    window_hours: override model.yaml window (used by --backfill-days).
    limit: when set, raises/sets the selection cap for this run (not clamped down).
    After a live run, automatically rescores recent fallbacks (last N days) unless
    ``skip_auto_rescore`` is set.
    use_fallback_model: run the whole job on config fallback_model (one-off backfill).
    """
    model_cfg = load_model_config()
    if window_hours is not None:
        model_cfg = {**model_cfg, "window_hours": int(window_hours)}
    max_per_run = int(model_cfg["max_items_per_run"])
    # WHY: backfill passes --limit to raise the cap, daily runs use model.yaml default.
    effective_limit = max_per_run if limit is None else max(1, int(limit))
    try:
        model_id = resolve_pipeline_model(model_cfg, use_fallback=use_fallback_model)
    except ValueError as exc:
        return PipelineResult(
            status="failed",
            run_id=None,
            message=str(exc),
        )
    # When --use-fallback-model, treat fallback as the only active model for this run.
    active_is_fallback_flag = bool(use_fallback_model)
    fallback_model = str(model_cfg.get("fallback_model") or "").strip()
    relevance_cfg = load_relevance_config()
    source_meta = _source_meta_by_id()
    use_turso = turso_configured() and db_path is None
    path = None if use_turso else (Path(db_path) if db_path else DEFAULT_DB_PATH)

    # Fetch already drops out-of-window raw entries (important for huge archives).
    fetch_result = fetch_and_normalize(
        timeout=float(model_cfg.get("fetch_timeout_seconds", 15)),
        window_hours=int(model_cfg["window_hours"]),
    )
    items_fetched = len(fetch_result.entries)
    # Only required-source failures affect status=partial.
    source_errors = len(fetch_result.errors)
    source_warnings = len(getattr(fetch_result, "warnings", []) or [])

    if dry_run:
        return _run_dry(
            fetch_result_entries=fetch_result.entries,
            fetch_errors=fetch_result.errors,
            items_fetched=items_fetched,
            source_errors=source_errors,
            source_warnings=source_warnings,
            model_cfg=model_cfg,
            relevance_cfg=relevance_cfg,
            source_meta=source_meta,
            effective_limit=effective_limit,
            use_turso=use_turso,
            path=path,
        )

    if use_turso:
        init_db()
    else:
        assert path is not None
        init_db(path)
    weights = load_weights()
    run_id: str | None = None
    items_scored = 0
    items_failed = 0
    items_fallback = 0
    items_filtered = 0
    items_classified_ok = 0
    retries_used_total = 0
    classify_errors: list[str] = []

    def _connect():
        return get_connection() if use_turso else get_connection(path)

    try:
        conn = _connect()
        try:
            repo = Repository(conn)
            run_id = repo.start_run(trigger)

            new_entries = _filter_entries_to_process(repo, fetch_result.entries)
            selected, filtered, _cap_skipped, in_window = _apply_freshness_gate_select(
                new_entries,
                model_cfg=model_cfg,
                relevance_cfg=relevance_cfg,
                source_meta=source_meta,
                effective_limit=effective_limit,
            )

            for entry, reason in filtered:
                try:
                    _persist_filtered(repo, entry, reason, run_id=run_id)
                    items_filtered += 1
                except Exception as exc:  # noqa: BLE001
                    items_failed += 1
                    classify_errors.append(f"{entry.url}: filter persist failed: {exc}")

            classified_by_source: dict[str, int] = {}
            batch_size = max(1, int(model_cfg.get("batch_size", 5)))
            source_kinds = {
                sid: str(meta.get("kind") or "")
                for sid, meta in source_meta.items()
            }
            active_model = model_id
            configure_llm_interval(model_min_interval_seconds(model_cfg, active_model))
            usage_guard = LlmUsageGuard(
                repo, model_cfg, purpose="pipeline", model_id=active_model
            )
            items_pending_scoring = 0
            daily_quota_hit = False
            quota_reason = "daily quota"

            # Batched classification (one Gemini call per chunk). Missing/invalid
            # ids are retried individually inside classify_entries_batch_with_fallback.
            remaining_batches = chunk_entries(selected, batch_size)
            batch_idx = 0
            while batch_idx < len(remaining_batches):
                batch = remaining_batches[batch_idx]
                batch_idx += 1
                try:
                    batch_outcomes = classify_entries_batch_with_fallback(
                        batch,
                        model_config=model_cfg,
                        source_kinds=source_kinds,
                        usage_guard=usage_guard,
                        model_id_override=active_model,
                    )
                except DailyQuotaError as exc:
                    # Prefer switching to fallback_model once if configured and not already on it.
                    if (
                        not active_is_fallback_flag
                        and fallback_model
                        and active_model != fallback_model
                    ):
                        logger.warning(
                            "Primary model %s hit daily quota (%s), switching to fallback_model=%s",
                            active_model,
                            exc.retry_hint or "no hint",
                            fallback_model,
                        )
                        active_model = fallback_model
                        active_is_fallback_flag = True
                        configure_llm_interval(
                            model_min_interval_seconds(model_cfg, active_model)
                        )
                        usage_guard = LlmUsageGuard(
                            repo, model_cfg, purpose="pipeline", model_id=active_model
                        )
                        # Retry this same batch on the fallback model.
                        batch_idx -= 1
                        continue
                    daily_quota_hit = True
                    hint = exc.retry_hint
                    if hint:
                        logger.warning("Daily quota retry hint: %s", hint)
                    # Current batch + all not-yet-processed batches → pending_scoring.
                    pending_entries = list(batch)
                    for later in remaining_batches[batch_idx:]:
                        pending_entries.extend(later)
                    for entry in pending_entries:
                        try:
                            _persist_pending_scoring(repo, entry, run_id=run_id)
                            items_pending_scoring += 1
                        except Exception as persist_exc:  # noqa: BLE001
                            items_failed += 1
                            classify_errors.append(
                                f"{entry.url}: pending persist failed: {persist_exc}"
                            )
                    break
                except Exception as exc:  # noqa: BLE001
                    items_failed += len(batch)
                    classify_errors.append(f"classify batch failed: {exc}")
                    continue

                for entry, result, used_fallback, err, retries_used in batch_outcomes:
                    try:
                        _persist_classified(
                            repo,
                            entry,
                            result,
                            weights=weights,
                            model_id=active_model,
                            run_id=run_id,
                            is_fallback=used_fallback,
                        )
                        items_scored += 1
                        retries_used_total += int(retries_used)
                        classified_by_source[entry.source_id] = (
                            classified_by_source.get(entry.source_id, 0) + 1
                        )
                        if used_fallback:
                            items_fallback += 1
                            if err:
                                classify_errors.append(f"{entry.url}: {err}")
                        else:
                            items_classified_ok += 1
                    except Exception as exc:  # noqa: BLE001
                        items_failed += 1
                        classify_errors.append(f"{entry.url}: persist failed: {exc}")
                        continue

            try:
                repo.save_source_run_stats(
                    _build_source_run_stat_rows(
                        run_id=run_id,
                        fetch_stats=list(fetch_result.source_stats),
                        new_entries=new_entries,
                        in_window=in_window,
                        filtered=filtered,
                        selected=selected,
                        classified_by_source=classified_by_source,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - telemetry must not kill the run
                logger.warning("Failed to persist source_run_stats: %s", exc)

            status = _resolve_status(
                items_fetched=items_fetched,
                items_new=len(new_entries),
                items_scored=items_scored,
                items_failed=items_failed,
                items_fallback=items_fallback,
                source_errors=source_errors,
                attempted=len(selected),
            )
            if daily_quota_hit:
                status = "degraded"
            error_message = None
            if daily_quota_hit:
                error_message = (
                    f"{quota_reason}: {items_pending_scoring} item(s) left "
                    f"pending_scoring (not fallback), model={active_model}"
                )
            elif (
                len(selected) > 0
                and items_fallback >= len(selected)
                and items_fallback > 0
            ):
                error_message = (
                    f"Model call failed for every selected item "
                    f"({items_fallback}/{len(selected)} fallback)"
                    + (
                        f", first: {classify_errors[0][:160]}"
                        if classify_errors
                        else ""
                    )
                )
            elif items_failed and classify_errors:
                error_message = (
                    f"{items_failed} item(s) failed to persist "
                    f"(first: {classify_errors[0][:200]})"
                )
            elif items_fallback and items_scored > 0:
                error_message = (
                    f"{items_fallback} item(s) saved with average fallback scores "
                    f"(Gemini unavailable)"
                    + (
                        f", first: {classify_errors[0][:120]}"
                        if classify_errors
                        else ""
                    )
                )
            elif len(new_entries) == 0 and items_scored == 0 and len(selected) == 0:
                error_message = (
                    "No new articles to ingest - everything in the feed is already in the digest."
                )
            elif source_errors and status != "success":
                error_message = f"{source_errors} source fetch error(s)"
            if source_warnings:
                warn_note = f"{source_warnings} source warning(s) (non-required / soft failures)"
                error_message = (
                    f"{error_message} {warn_note}".strip()
                    if error_message
                    else warn_note
                )

            rescored_ok = 0
            rescored_fallback = 0
            if not skip_auto_rescore and not daily_quota_hit:
                # Heal recent fallbacks from temporary model outages (incl. this run).
                # Skipped on daily-quota stop so we do not burn/loop the exhausted model.
                rescore_stats = rescore_fallback_items(
                    repo,
                    model_cfg=model_cfg,
                    weights=weights,
                    source_meta=source_meta,
                    model_id_override=active_model,
                    usage_guard=usage_guard,
                )
                rescored_ok = rescore_stats.ok
                rescored_fallback = rescore_stats.still_fallback
                retries_used_total += rescore_stats.retries_used
                if rescore_stats.attempted:
                    heal_note = (
                        f" Auto-rescore: attempted={rescore_stats.attempted} "
                        f"ok={rescore_stats.ok} still_fallback={rescore_stats.still_fallback}."
                    )
                    error_message = (error_message or "Pipeline completed.") + heal_note
            elif daily_quota_hit:
                error_message = (error_message or "") + " Auto-rescore skipped (daily quota)."

            repo.finish_run(
                run_id,
                status=status,
                items_fetched=items_fetched,
                items_new=len(new_entries),
                items_scored=items_scored,
                items_classified_ok=items_classified_ok,
                items_fallback=items_fallback,
                retries_used=retries_used_total,
                error_message=error_message,
            )

            return PipelineResult(
                status=status,
                run_id=run_id,
                items_fetched=items_fetched,
                items_new=len(new_entries),
                items_scored=items_scored,
                items_failed=items_failed,
                items_filtered=items_filtered,
                items_fallback=items_fallback,
                items_classified_ok=items_classified_ok,
                retries_used=retries_used_total,
                items_rescored_ok=rescored_ok,
                items_rescored_fallback=rescored_fallback,
                source_errors=source_errors,
                source_warnings=source_warnings,
                dry_run=False,
                message=error_message or "Pipeline completed.",
                new_entries=selected,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001 - top-level guard for run table status
        message = f"Pipeline failed: {exc}"
        if run_id is not None:
            try:
                conn = _connect()
                try:
                    Repository(conn).finish_run(
                        run_id,
                        status="failed",
                        items_fetched=items_fetched,
                        items_new=0,
                        items_scored=items_scored,
                        error_message=message[:500],
                    )
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                pass
        return PipelineResult(
            status="failed",
            run_id=run_id,
            items_fetched=items_fetched,
            items_scored=items_scored,
            items_failed=items_failed,
            items_filtered=items_filtered,
            source_errors=source_errors,
            message=message,
        )


def _run_dry(
    *,
    fetch_result_entries: list[NormalizedEntry],
    fetch_errors: list[Any],
    items_fetched: int,
    source_errors: int,
    source_warnings: int = 0,
    model_cfg: dict[str, Any],
    relevance_cfg: dict[str, Any],
    source_meta: dict[str, dict[str, Any]],
    effective_limit: int,
    use_turso: bool,
    path: Path | None,
) -> PipelineResult:
    """Fetch + dedupe + freshness + gate + select + print, keep DB conn open until done."""
    existing_urls: set[str] = set()
    existing_hashes: set[str] = set()
    dry_repo: Repository | None = None
    conn: Any | None = None
    try:
        if use_turso:
            conn = get_connection()
            dry_repo = Repository(conn)
            existing_urls = dry_repo.existing_urls()
            existing_hashes = dry_repo.existing_content_hashes()
        elif path is not None and path.exists():
            conn = get_connection(path)
            dry_repo = Repository(conn)
            existing_urls = dry_repo.existing_urls()
            existing_hashes = dry_repo.existing_content_hashes()

        if dry_repo is not None:
            new_entries = _filter_entries_to_process(dry_repo, fetch_result_entries)
        else:
            new_entries = _filter_new_entries_simple(
                fetch_result_entries, existing_urls, existing_hashes
            )

        selected, filtered, cap_skipped, _in_window = _apply_freshness_gate_select(
            new_entries,
            model_cfg=model_cfg,
            relevance_cfg=relevance_cfg,
            source_meta=source_meta,
            effective_limit=effective_limit,
        )

        print(
            f"[dry-run] fetched={items_fetched} new={len(new_entries)} "
            f"filtered={len(filtered)} cap_skipped={len(cap_skipped)} "
            f"would_classify={len(selected)} source_errors={source_errors} "
            f"source_warnings={source_warnings} "
            f"limit={effective_limit} window_hours={model_cfg['window_hours']}"
        )
        if fetch_errors:
            print(f"[dry-run] source errors ({source_errors}):")
            for err in fetch_errors[:10]:
                print(f"  - {err.source_id}: {err.message}")
        for entry in selected:
            print(f"  [{entry.competitor}/{entry.source_id}] {entry.title}")
            print(f"    {entry.url}")

        if not fetch_result_entries and source_errors:
            status: RunStatus = "failed"
        elif source_errors:
            status = "partial"
        else:
            status = "success"

        return PipelineResult(
            status=status,
            run_id=None,
            items_fetched=items_fetched,
            items_new=len(new_entries),
            items_scored=0,
            items_filtered=len(filtered),
            source_errors=source_errors,
            source_warnings=source_warnings,
            dry_run=True,
            message="Dry run complete (no LLM, no DB writes).",
            new_entries=selected,
        )
    except Exception as exc:  # noqa: BLE001
        # Surface Turso/connectivity failures instead of panicking after close.
        logger.exception("Dry-run failed")
        return PipelineResult(
            status="failed",
            run_id=None,
            items_fetched=items_fetched,
            source_errors=source_errors,
            dry_run=True,
            message=f"Dry run failed: {exc}",
        )
    finally:
        # WHY: closing before selection reused a dead Turso handle (panic in --dry-run).
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _resolve_status(
    *,
    items_fetched: int,
    items_new: int,
    items_scored: int,
    items_failed: int,
    items_fallback: int = 0,
    source_errors: int,
    attempted: int,
    fallback_degraded_ratio: float = 0.30,
) -> RunStatus:
    if attempted == 0:
        if items_fetched == 0 and source_errors > 0:
            return "failed"
        if source_errors > 0:
            return "partial"
        return "success"
    if items_scored == 0 and items_failed > 0:
        return "failed"
    # Every model call failed → hard failure (no silent success for cron/GHA).
    if attempted > 0 and items_fallback >= attempted and items_fallback > 0:
        return "failed"
    fallback_ratio = items_fallback / attempted if attempted else 0.0
    if fallback_ratio > fallback_degraded_ratio:
        return "degraded"
    if items_failed > 0 or source_errors > 0 or items_fallback > 0:
        return "partial"
    return "success"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the daily CI Intel ingestion + Gemini classification pipeline."
    )
    parser.add_argument(
        "--trigger",
        choices=("manual", "cron"),
        default="manual",
        help="How this run was started (stored on pipeline_runs).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"SQLite path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Selection/classify cap for this run (overrides max_items_per_run, can raise it).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + dedupe + freshness + gate + select + print, no LLM, no DB writes.",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=None,
        help="Widen the freshness window to N days for a real backfill ingest.",
    )
    parser.add_argument(
        "--rescore-fallbacks",
        action="store_true",
        help="Re-classify stored is_fallback / pending_scoring items instead of ingesting.",
    )
    parser.add_argument(
        "--use-fallback-model",
        action="store_true",
        help="Run the whole job on config fallback_model (for backfill when primary quota is small).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.rescore_fallbacks:
        result = run_rescore_only(
            db_path=args.db,
            limit=args.limit,
            use_fallback_model=args.use_fallback_model,
            trigger=args.trigger,
        )
        print(
            f"status={result.status} rescored_ok={result.items_rescored_ok} "
            f"still_fallback={result.items_rescored_fallback} "
            f"retries={result.retries_used}"
            + (f" run_id={result.run_id}" if result.run_id else "")
        )
        if result.message:
            print(result.message)
        # Nightly retry: quota/degraded is NOT a failure (items stay pending).
        # Only hard failures fail the process / GHA job.
        return 1 if result.status == "failed" else 0

    window_hours = None
    if args.backfill_days is not None:
        if args.backfill_days < 1:
            print("--backfill-days must be >= 1", file=sys.stderr)
            return 2
        window_hours = int(args.backfill_days) * 24
    result = run_daily(
        trigger=args.trigger,
        db_path=args.db,
        limit=args.limit,
        dry_run=args.dry_run,
        window_hours=window_hours,
        use_fallback_model=args.use_fallback_model,
    )
    print(
        f"status={result.status} fetched={result.items_fetched} "
        f"new={result.items_new} scored={result.items_scored} "
        f"ok={result.items_classified_ok} fallback={result.items_fallback} "
        f"retries={result.retries_used} "
        f"rescored_ok={result.items_rescored_ok} "
        f"filtered={result.items_filtered} "
        f"failed={result.items_failed} source_errors={result.source_errors}"
        + (f" run_id={result.run_id}" if result.run_id else "")
    )
    if result.message:
        print(result.message)
    if result.status in ("failed", "degraded"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
