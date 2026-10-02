"""Daily competitive-intelligence pipeline orchestration.

Flow: init_db → start_run → fetch → dedupe → classify+score (capped) → persist → finish_run.

Per-item LLM failures do not abort the run (status becomes partial). Cost is
guarded by max_items_per_run from config/model.yaml.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from src.config_loader import DEFAULT_DB_PATH, load_model_config, load_weights
from src.db.connection import get_connection, init_db, turso_configured
from src.db.models import DimensionScores, NewsItem
from src.db.repository import Repository
from src.ingest.normalize import NormalizedEntry
from src.ingest.rss_fetcher import fetch_and_normalize
from src.process.dedupe import is_duplicate
from src.process.llm_classify import ClassifyError, ClassificationResult, classify_entry
from src.process.scoring import weighted_score

RunTrigger = Literal["manual", "cron"]
RunStatus = Literal["success", "partial", "failed"]


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
    source_errors: int = 0
    dry_run: bool = False
    message: str = ""
    new_entries: list[NormalizedEntry] = field(default_factory=list)


def _filter_new_entries(
    entries: list[NormalizedEntry],
    existing_urls: set[str],
    existing_hashes: set[str],
) -> list[NormalizedEntry]:
    """Drop items already seen by URL or content hash; track seen within-batch too."""
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


def _persist_classified(
    repo: Repository,
    entry: NormalizedEntry,
    result: ClassificationResult,
    *,
    weights: dict[str, float],
    model_id: str,
    run_id: str,
) -> None:
    score = weighted_score(result.dimension_dict(), weights)
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
    )
    dims = DimensionScores(
        jfrog_relevance=result.jfrog_relevance,
        competitor_signal=result.competitor_signal,
        strategic_impact=result.strategic_impact,
        freshness=result.freshness,
        market_visibility=result.market_visibility,
        model_id=model_id,
        scored_at=now,
    )
    repo.upsert_news_item(item)
    repo.save_dimension_scores(news_id, dims)


def run_daily(
    *,
    trigger: RunTrigger = "manual",
    db_path: Path | str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> PipelineResult:
    """Execute one daily ingestion + classification cycle.

    dry_run: fetch + dedupe + print only — no LLM calls and no DB writes.
    """
    model_cfg = load_model_config()
    max_per_run = int(model_cfg["max_items_per_run"])
    # WHY cap: prevents runaway Gemini spend if feeds suddenly flood with items.
    effective_limit = max_per_run if limit is None else min(limit, max_per_run)
    model_id = str(model_cfg["model_id"])
    use_turso = turso_configured() and db_path is None
    path = None if use_turso else (Path(db_path) if db_path else DEFAULT_DB_PATH)

    fetch_result = fetch_and_normalize()
    items_fetched = len(fetch_result.entries)
    source_errors = len(fetch_result.errors)

    if dry_run:
        # Dry-run still needs existing DB state for realistic dedupe if present,
        # but never writes and never calls the LLM.
        existing_urls: set[str] = set()
        existing_hashes: set[str] = set()
        try:
            if use_turso:
                conn = get_connection()
                try:
                    repo = Repository(conn)
                    existing_urls = repo.existing_urls()
                    existing_hashes = repo.existing_content_hashes()
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            elif path is not None and path.exists():
                with get_connection(path) as conn:
                    repo = Repository(conn)
                    existing_urls = repo.existing_urls()
                    existing_hashes = repo.existing_content_hashes()
        except Exception:
            pass

        new_entries = _filter_new_entries(
            fetch_result.entries, existing_urls, existing_hashes
        )
        to_process = new_entries[:effective_limit]
        print(
            f"[dry-run] fetched={items_fetched} new={len(new_entries)} "
            f"would_classify={len(to_process)} source_errors={source_errors} "
            f"limit={effective_limit}"
        )
        if fetch_result.errors:
            print(f"[dry-run] source errors ({source_errors}):")
            for err in fetch_result.errors[:10]:
                print(f"  - {err.source_id}: {err.message}")
        for entry in to_process:
            print(f"  [{entry.competitor}/{entry.source_id}] {entry.title}")
            print(f"    {entry.url}")

        if not fetch_result.entries and source_errors:
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
            source_errors=source_errors,
            dry_run=True,
            message="Dry run complete (no LLM, no DB writes).",
            new_entries=to_process,
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
    classify_errors: list[str] = []

    def _connect():
        return get_connection() if use_turso else get_connection(path)

    try:
        conn = _connect()
        try:
            repo = Repository(conn)
            run_id = repo.start_run(trigger)

            new_entries = _filter_new_entries(
                fetch_result.entries,
                repo.existing_urls(),
                repo.existing_content_hashes(),
            )
            to_process = new_entries[:effective_limit]

            for entry in to_process:
                try:
                    result = classify_entry(entry, model_config=model_cfg)
                    _persist_classified(
                        repo,
                        entry,
                        result,
                        weights=weights,
                        model_id=model_id,
                        run_id=run_id,
                    )
                    items_scored += 1
                except ClassifyError as exc:
                    # Per-item failure must not abort the whole run.
                    items_failed += 1
                    classify_errors.append(f"{entry.url}: {exc}")
                    continue

            status = _resolve_status(
                items_fetched=items_fetched,
                items_new=len(new_entries),
                items_scored=items_scored,
                items_failed=items_failed,
                source_errors=source_errors,
                attempted=len(to_process),
            )
            error_message = None
            if classify_errors:
                # Keep message short and non-sensitive for the runs table.
                error_message = (
                    f"{items_failed} item(s) failed classification "
                    f"(first: {classify_errors[0][:200]})"
                )
            elif source_errors and status != "success":
                error_message = f"{source_errors} source fetch error(s)"

            repo.finish_run(
                run_id,
                status=status,
                items_fetched=items_fetched,
                items_new=len(new_entries),
                items_scored=items_scored,
                error_message=error_message,
            )

            return PipelineResult(
                status=status,
                run_id=run_id,
                items_fetched=items_fetched,
                items_new=len(new_entries),
                items_scored=items_scored,
                items_failed=items_failed,
                source_errors=source_errors,
                dry_run=False,
                message=error_message or "Pipeline completed.",
                new_entries=to_process,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001 — top-level guard for run table status
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
            source_errors=source_errors,
            message=message,
        )


def _resolve_status(
    *,
    items_fetched: int,
    items_new: int,
    items_scored: int,
    items_failed: int,
    source_errors: int,
    attempted: int,
) -> RunStatus:
    if attempted == 0:
        # Nothing to classify — success unless every source failed with zero entries.
        if items_fetched == 0 and source_errors > 0:
            return "failed"
        if source_errors > 0:
            return "partial"
        return "success"
    if items_scored == 0 and items_failed > 0:
        return "failed"
    if items_failed > 0 or source_errors > 0:
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
        help="Max new items to classify (also capped by model.yaml max_items_per_run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + dedupe + print only; no LLM calls and no DB writes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = run_daily(
        trigger=args.trigger,
        db_path=args.db,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(
        f"status={result.status} fetched={result.items_fetched} "
        f"new={result.items_new} scored={result.items_scored} "
        f"failed={result.items_failed} source_errors={result.source_errors}"
        + (f" run_id={result.run_id}" if result.run_id else "")
    )
    if result.message:
        print(result.message)
    if result.status == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
