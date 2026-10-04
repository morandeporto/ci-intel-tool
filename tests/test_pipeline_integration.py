"""End-to-end pipeline integration tests (no network, no live Gemini).

Uses fixture RSS XML via a mocked httpx client, a mocked Gemini generate path,
and a temporary SQLite DB created through the normal init/migrate path.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape

import httpx
import pytest

from src.db.connection import get_connection, init_db
from src.pipeline.run_daily import (
    exit_code_for_live_run,
    run_daily,
    run_rescore_only,
)
from src.process.llm_quota import DailyQuotaError

# Fixed clock so pubDates stay inside/outside the 48h window deterministically.
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)

SNYK_URL = "https://feeds.test/snyk.xml"
COMMUNITY_URL = "https://feeds.test/community.xml"
REQUIRED_FAIL_URL = "https://feeds.test/required-fail.xml"
COMMUNITY_FAIL_URL = "https://feeds.test/community-fail.xml"
OK_OFFICIAL_URL = "https://feeds.test/ok-official.xml"

FIXTURE_SOURCES_HAPPY = [
    {
        "id": "snyk_blog",
        "competitor": "snyk",
        "kind": "official_competitor",
        "gate": "off",
        "type": "rss",
        "url": SNYK_URL,
        "enabled": True,
        "required": True,
        "user_agent": "honest",
    },
    {
        "id": "community_noise",
        "competitor": None,
        "kind": "community",
        "gate": "strict",
        "type": "rss",
        "url": COMMUNITY_URL,
        "enabled": True,
        "required": False,
        "user_agent": "honest",
    },
]

FIXTURE_SOURCES_FAILURE = [
    {
        "id": "sonatype_blog",
        "competitor": "sonatype",
        "kind": "official_competitor",
        "gate": "off",
        "type": "rss",
        "url": REQUIRED_FAIL_URL,
        "enabled": True,
        "required": True,
        "user_agent": "honest",
    },
    {
        "id": "community_noise",
        "competitor": None,
        "kind": "community",
        "gate": "strict",
        "type": "rss",
        "url": COMMUNITY_FAIL_URL,
        "enabled": True,
        "required": False,
        "user_agent": "honest",
    },
    {
        "id": "snyk_blog",
        "competitor": "snyk",
        "kind": "official_competitor",
        "gate": "off",
        "type": "rss",
        "url": OK_OFFICIAL_URL,
        "enabled": True,
        "required": True,
        "user_agent": "honest",
    },
]


def _pub(hours_ago: float | None = None, *, hours_ahead: float | None = None) -> str:
    if hours_ahead is not None:
        dt = NOW + timedelta(hours=hours_ahead)
    else:
        dt = NOW - timedelta(hours=float(hours_ago or 0))
    return format_datetime(dt)


def _rss_feed(items: list[dict[str, str]]) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel><title>Fixture Feed</title>',
    ]
    for item in items:
        parts.append(
            "<item>"
            f"<title>{escape(item['title'])}</title>"
            f"<link>{escape(item['link'])}</link>"
            f"<pubDate>{escape(item['pubDate'])}</pubDate>"
            f"<description>{escape(item.get('description', ''))}</description>"
            "</item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


def _base_snyk_edge_items(*, include_cap_overflow: bool) -> list[dict[str, str]]:
    """Shared official items. Cap overflow is only for the happy-path selection check."""
    items = [
        {
            "title": "OFFICIAL_NEW_1",
            "link": "https://example.com/snyk/new-1",
            "pubDate": _pub(1),
            "description": "Snyk product launch update",
        },
        {
            "title": "OFFICIAL_NEW_2",
            "link": "https://example.com/snyk/new-2",
            "pubDate": _pub(2),
            "description": "Snyk SCA improvement",
        },
        {
            "title": "OFFICIAL_NEW_3",
            "link": "https://example.com/snyk/new-3",
            "pubDate": _pub(3),
            "description": "Snyk platform note",
        },
    ]
    if include_cap_overflow:
        # Fourth in-window item → max_per_source=3 leaves this unselected (not persisted).
        items.append(
            {
                "title": "OFFICIAL_CAP_SKIP",
                "link": "https://example.com/snyk/cap-skip",
                "pubDate": _pub(4),
                "description": "Older official item beyond per-source cap",
            }
        )
    items.extend(
        [
            # Outside 48h window → dropped at fetch, never persisted.
            {
                "title": "OFFICIAL_TOO_OLD",
                "link": "https://example.com/snyk/too-old",
                "pubDate": _pub(72),
                "description": "Archive item",
            },
            # Future-dated → dropped at fetch.
            {
                "title": "OFFICIAL_FUTURE",
                "link": "https://example.com/snyk/future",
                "pubDate": _pub(hours_ahead=6),
                "description": "Misdated future item",
            },
            # Exclude-title pattern → status=filtered.
            {
                "title": "Planned Cloud Maintenance for EU region",
                "link": "https://example.com/snyk/maintenance",
                "pubDate": _pub(5),
                "description": "Maintenance window notice",
            },
        ]
    )
    return items


def _community_items() -> list[dict[str, str]]:
    return [
        # Strict gate pass (strong keyword).
        {
            "title": "COMMUNITY_SBOM_HIT",
            "link": "https://example.com/community/sbom",
            "pubDate": _pub(2),
            "description": "Discussion of a new SBOM mandate",
        },
        # Strict gate fail (no strong keyword).
        {
            "title": "COMMUNITY_NOISE_MISS",
            "link": "https://example.com/community/noise",
            "pubDate": _pub(1),
            "description": "Weekend cooking tips and travel photos",
        },
    ]


def _happy_feeds() -> dict[str, bytes]:
    """Official + community mix covering window/gate/cap edge cases."""
    return {
        SNYK_URL: _rss_feed(_base_snyk_edge_items(include_cap_overflow=True)),
        COMMUNITY_URL: _rss_feed(_community_items()),
    }


def _stable_feeds() -> dict[str, bytes]:
    """Same mix without cap overflow so a second run has nothing left to ingest."""
    return {
        SNYK_URL: _rss_feed(_base_snyk_edge_items(include_cap_overflow=False)),
        COMMUNITY_URL: _rss_feed(_community_items()),
    }


def _ok_official_feed() -> bytes:
    return _rss_feed(
        [
            {
                "title": "SNYK_STILL_INGESTS",
                "link": "https://example.com/snyk/ok-1",
                "pubDate": _pub(1),
                "description": "Healthy official item after sibling source failure",
            }
        ]
    )


def _classification_payload(item_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": f"Fixture summary for {item_id or 'item'}.",
        "category": "product_release",
        "item_type": "competitor",
        "jfrog_implication": "Fixture implication tied to the excerpt.",
        "jfrog_relevance": 4,
        "competitor_signal": 4,
        "strategic_impact": 3,
        "freshness": 4,
        "market_visibility": 3,
    }
    if item_id is not None:
        payload["id"] = item_id
    return payload


def _batch_response_for_prompt(prompt: str) -> str:
    ids = re.findall(r"=== ITEM id=([^\s=]+) ===", prompt)
    return json.dumps({"items": [_classification_payload(i) for i in ids]})


class _FakeResponse:
    def __init__(self, url: str, status_code: int, content: bytes) -> None:
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "application/rss+xml"}
        self.request = httpx.Request("GET", url)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


def _install_http_mock(
    monkeypatch: pytest.MonkeyPatch,
    body_by_url: dict[str, bytes],
    *,
    status_by_url: dict[str, int] | None = None,
) -> None:
    status_by_url = status_by_url or {}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            del kwargs
            status = int(status_by_url.get(url, 200))
            content = body_by_url.get(url, b"")
            return _FakeResponse(url, status, content)

        def close(self) -> None:
            return None

    monkeypatch.setattr("src.ingest.rss_fetcher.httpx.Client", FakeClient)


def _fast_model_cfg(**overrides: Any) -> dict[str, Any]:
    from src.config_loader import load_model_config

    cfg = dict(load_model_config())
    cfg.update(
        {
            "rate_limit_sleep_seconds": 0,
            "llm_min_interval_seconds": 0,
            "model_min_interval_seconds": {
                "gemini-3.8-flash": 0,
                "gemini-3.5-flash-lite": 0,
            },
            "llm_max_attempts": 1,
            "llm_retry_base_seconds": 0,
            "llm_retry_max_seconds": 0,
            "fetch_timeout_seconds": 1,
            "fetch_concurrency": 4,
            "batch_size": 2,
            "max_per_source": 3,
            "max_items_per_run": 20,
            "window_hours": 48,
            "fallback_model": "",
            "model_daily_limits": {
                "gemini-3.8-flash": 1000,
                "gemini-3.5-flash-lite": 1000,
            },
        }
    )
    cfg.update(overrides)
    return cfg


def _install_pipeline_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sources: list[dict[str, Any]],
    feeds: dict[str, bytes],
    status_by_url: dict[str, int] | None = None,
    model_overrides: dict[str, Any] | None = None,
    gemini: Callable[..., str] | None = None,
) -> dict[str, int]:
    """Wire sources, HTTP, model config, clock, sleeps, and Gemini mock."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    # Never accidentally hit Turso during integration tests.
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)

    monkeypatch.setattr(
        "src.pipeline.run_daily.load_sources", lambda *a, **k: list(sources)
    )
    monkeypatch.setattr(
        "src.ingest.rss_fetcher.enabled_sources", lambda *a, **k: list(sources)
    )
    monkeypatch.setattr(
        "src.config_loader.enabled_sources", lambda *a, **k: list(sources)
    )

    cfg = _fast_model_cfg(**(model_overrides or {}))
    monkeypatch.setattr("src.pipeline.run_daily.load_model_config", lambda: dict(cfg))
    monkeypatch.setattr("src.process.llm_classify.load_model_config", lambda: dict(cfg))
    monkeypatch.setattr("src.ingest.rss_fetcher.load_model_config", lambda: dict(cfg))

    _install_http_mock(monkeypatch, feeds, status_by_url=status_by_url)

    # Freeze fetch/freshness clocks to NOW.
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            if tz is None:
                return NOW.replace(tzinfo=None)
            return NOW.astimezone(tz)

    monkeypatch.setattr("src.ingest.rss_fetcher.datetime", _FrozenDateTime)
    monkeypatch.setattr("src.process.freshness.datetime", _FrozenDateTime)
    monkeypatch.setattr("src.pipeline.run_daily.datetime", _FrozenDateTime)

    monkeypatch.setattr("src.ingest.rss_fetcher.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("src.process.llm_classify.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("src.process.retry.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "src.process.llm_rate_limit.wait_llm_interval", lambda: None
    )

    calls = {"n": 0}

    def _default_gemini(*, prompt: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        calls["n"] += 1
        name = getattr(response_schema, "__name__", "") if response_schema else ""
        if name == "BatchedClassificationResponse":
            return _batch_response_for_prompt(prompt)
        return json.dumps(_classification_payload())

    monkeypatch.setattr(
        "src.process.llm_classify._gemini_generate",
        gemini or _default_gemini,
    )
    return calls


def _open_repo(db_path: Path):
    conn = get_connection(db_path)
    return conn


def _all_news(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, title, url, source_id, status, filter_reason,
               relevance_score, is_fallback, content_hash
        FROM news_items
        ORDER BY title
        """
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _by_title(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r["title"]): r for r in rows}


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------
def test_pipeline_happy_path_statuses_cap_and_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mix of official/community items; assert DB statuses, cap, stats, success."""
    db_path = tmp_path / "happy.db"
    init_db(db_path)
    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
    )

    result = run_daily(
        trigger="manual",
        db_path=db_path,
        skip_auto_rescore=True,
    )
    assert result.status == "success"
    assert result.run_id
    assert result.source_errors == 0

    conn = _open_repo(db_path)
    try:
        rows = _all_news(conn)
        by_title = _by_title(rows)

        # Out-of-window / future / cap-skipped never land in the DB.
        for missing in ("OFFICIAL_TOO_OLD", "OFFICIAL_FUTURE", "OFFICIAL_CAP_SKIP"):
            assert missing not in by_title

        for title in ("OFFICIAL_NEW_1", "OFFICIAL_NEW_2", "OFFICIAL_NEW_3"):
            assert by_title[title]["status"] == "classified"
            assert by_title[title]["relevance_score"] is not None
            assert int(by_title[title]["is_fallback"] or 0) == 0

        excl = by_title["Planned Cloud Maintenance for EU region"]
        assert excl["status"] == "filtered"
        assert str(excl["filter_reason"]).startswith("exclude_title_pattern:")

        assert by_title["COMMUNITY_SBOM_HIT"]["status"] == "classified"
        noise = by_title["COMMUNITY_NOISE_MISS"]
        assert noise["status"] == "filtered"
        assert noise["filter_reason"] == "no_strong_keyword"

        # Only selected items are classified; per-source cap respected.
        classified = [r for r in rows if r["status"] == "classified"]
        assert {r["title"] for r in classified} == {
            "OFFICIAL_NEW_1",
            "OFFICIAL_NEW_2",
            "OFFICIAL_NEW_3",
            "COMMUNITY_SBOM_HIT",
        }
        snyk_classified = [r for r in classified if r["source_id"] == "snyk_blog"]
        assert len(snyk_classified) == 3

        urls = [r["url"] for r in rows]
        assert len(urls) == len(set(urls))

        run = conn.execute(
            "SELECT status, error_message FROM pipeline_runs WHERE id = ?",
            (result.run_id,),
        ).fetchone()
        assert run is not None
        assert run[0] == "success"

        stats = conn.execute(
            "SELECT COUNT(*) FROM source_run_stats WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        assert stats is not None and int(stats[0]) >= 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Idempotence
# ---------------------------------------------------------------------------
def test_pipeline_idempotent_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second run on the same feeds must add nothing and create no duplicates."""
    db_path = tmp_path / "idem.db"
    init_db(db_path)
    # No cap-overflow item: unselected-but-unpersisted rows would be "new" next run.
    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_stable_feeds(),
    )

    first = run_daily(trigger="manual", db_path=db_path, skip_auto_rescore=True)
    assert first.status == "success"

    conn = _open_repo(db_path)
    try:
        before = _all_news(conn)
        before_urls = sorted(r["url"] for r in before)
        before_count = len(before)
    finally:
        conn.close()

    second = run_daily(trigger="manual", db_path=db_path, skip_auto_rescore=True)
    assert second.status == "success"
    assert second.items_new == 0
    assert second.items_scored == 0

    conn = _open_repo(db_path)
    try:
        after = _all_news(conn)
        assert len(after) == before_count
        assert sorted(r["url"] for r in after) == before_urls
        assert len({r["url"] for r in after}) == len(after)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Daily quota mid-run
# ---------------------------------------------------------------------------
def test_pipeline_daily_quota_leaves_pending_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PerDay quota mid-run → remaining items pending_scoring, degraded, exit 0."""
    db_path = tmp_path / "quota.db"
    init_db(db_path)

    def gemini(*, prompt: str, response_schema: Any = None, **kwargs: Any) -> str:
        # First batch succeeds; every later generate hits hard daily quota.
        if gemini.calls == 0:  # type: ignore[attr-defined]
            gemini.calls += 1  # type: ignore[attr-defined]
            name = getattr(response_schema, "__name__", "") if response_schema else ""
            if name == "BatchedClassificationResponse":
                return _batch_response_for_prompt(prompt)
            return json.dumps(_classification_payload())
        gemini.calls += 1  # type: ignore[attr-defined]
        raise DailyQuotaError(
            "429 Quota exceeded PerDay for model gemini-3.8-flash",
            model_id=str(kwargs.get("model_id") or "gemini-3.8-flash"),
        )

    gemini.calls = 0  # type: ignore[attr-defined]

    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
        model_overrides={"batch_size": 2, "fallback_model": ""},
        gemini=gemini,
    )

    result = run_daily(
        trigger="manual",
        db_path=db_path,
        skip_auto_rescore=True,
    )
    assert result.status == "degraded"
    assert "daily quota" in (result.message or "").lower()
    assert exit_code_for_live_run(result) == 0

    conn = _open_repo(db_path)
    try:
        rows = _all_news(conn)
        pending = [r for r in rows if r["status"] == "pending_scoring"]
        classified = [r for r in rows if r["status"] == "classified"]
        assert pending, "expected unscored remainder after quota stop"
        assert all(r["filter_reason"] == "daily_quota" for r in pending)
        assert all(int(r["is_fallback"] or 0) == 0 for r in pending)
        assert all(r["relevance_score"] is None for r in pending)
        assert classified, "first batch should have been classified before quota"
        # No mid-score fallbacks for the quota path.
        assert not any(int(r["is_fallback"] or 0) for r in rows)

        run = conn.execute(
            "SELECT status, error_message FROM pipeline_runs WHERE id = ?",
            (result.run_id,),
        ).fetchone()
        assert run is not None
        assert run[0] == "degraded"
        assert "daily quota" in str(run[1] or "").lower()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Rescore heals pending_scoring
# ---------------------------------------------------------------------------
def test_rescore_heals_pending_after_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a quota stop, rescore with a working mock classifies each pending once."""
    db_path = tmp_path / "rescore.db"
    init_db(db_path)

    def quota_then_ok(*, prompt: str, response_schema: Any = None, **kwargs: Any) -> str:
        del kwargs
        if quota_then_ok.phase == "quota":  # type: ignore[attr-defined]
            if quota_then_ok.calls == 0:  # type: ignore[attr-defined]
                quota_then_ok.calls += 1  # type: ignore[attr-defined]
                name = getattr(response_schema, "__name__", "") if response_schema else ""
                if name == "BatchedClassificationResponse":
                    return _batch_response_for_prompt(prompt)
                return json.dumps(_classification_payload())
            raise DailyQuotaError(
                "429 Quota exceeded PerDay",
                model_id="gemini-3.8-flash",
            )
        # Healing phase: always succeed.
        quota_then_ok.calls += 1  # type: ignore[attr-defined]
        name = getattr(response_schema, "__name__", "") if response_schema else ""
        if name == "BatchedClassificationResponse":
            return _batch_response_for_prompt(prompt)
        return json.dumps(_classification_payload())

    quota_then_ok.phase = "quota"  # type: ignore[attr-defined]
    quota_then_ok.calls = 0  # type: ignore[attr-defined]

    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
        model_overrides={"batch_size": 2, "fallback_model": ""},
        gemini=quota_then_ok,
    )

    degraded = run_daily(
        trigger="manual",
        db_path=db_path,
        skip_auto_rescore=True,
    )
    assert degraded.status == "degraded"

    conn = _open_repo(db_path)
    try:
        pending_before = [
            r for r in _all_news(conn) if r["status"] == "pending_scoring"
        ]
        pending_urls = {r["url"] for r in pending_before}
        assert pending_urls
        classified_before = {
            r["url"] for r in _all_news(conn) if r["status"] == "classified"
        }
    finally:
        conn.close()

    quota_then_ok.phase = "heal"  # type: ignore[attr-defined]
    quota_then_ok.calls = 0  # type: ignore[attr-defined]

    healed = run_rescore_only(db_path=db_path)
    assert healed.items_rescored_ok >= len(pending_urls)

    conn = _open_repo(db_path)
    try:
        rows = _all_news(conn)
        by_url = {r["url"]: r for r in rows}
        for url in pending_urls:
            row = by_url[url]
            assert row["status"] == "classified"
            assert row["relevance_score"] is not None
            assert row["filter_reason"] is None or row["filter_reason"] == ""
        # Previously classified URLs stay classified; no duplicate rows.
        assert len(rows) == len({r["url"] for r in rows})
        still_classified = {
            r["url"] for r in rows if r["status"] == "classified"
        }
        assert classified_before.issubset(still_classified)
        assert not any(r["status"] == "pending_scoring" for r in rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Source failure severity
# ---------------------------------------------------------------------------
def test_pipeline_source_failure_required_vs_community(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required 500 is an error; community 502 is a warning; other sources ingest."""
    db_path = tmp_path / "srcfail.db"
    init_db(db_path)
    feeds = {
        REQUIRED_FAIL_URL: b"",
        COMMUNITY_FAIL_URL: b"",
        OK_OFFICIAL_URL: _ok_official_feed(),
    }
    _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_FAILURE,
        feeds=feeds,
        status_by_url={
            REQUIRED_FAIL_URL: 500,
            COMMUNITY_FAIL_URL: 502,
            OK_OFFICIAL_URL: 200,
        },
    )

    result = run_daily(
        trigger="manual",
        db_path=db_path,
        skip_auto_rescore=True,
    )
    # Run continues; required failure → partial (not hard-failed if others scored).
    assert result.status in ("partial", "success", "degraded")
    assert result.source_errors >= 1
    assert result.source_warnings >= 1

    conn = _open_repo(db_path)
    try:
        rows = _all_news(conn)
        titles = {r["title"] for r in rows}
        assert "SNYK_STILL_INGESTS" in titles

        stats = conn.execute(
            """
            SELECT source_id, error, warning, http_status
            FROM source_run_stats
            WHERE run_id = ?
            """,
            (result.run_id,),
        ).fetchall()
        by_sid = {r[0]: r for r in stats}
        assert by_sid["sonatype_blog"][1], "required failure must be an error"
        assert not by_sid["sonatype_blog"][2]
        assert by_sid["community_noise"][2], "community failure must be a warning"
        assert not by_sid["community_noise"][1]
        assert by_sid["snyk_blog"][1] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Dry run
# ---------------------------------------------------------------------------
def test_pipeline_dry_run_no_writes_no_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run fetches and selects but must not write DB rows or call Gemini."""
    db_path = tmp_path / "dry.db"
    init_db(db_path)
    calls = _install_pipeline_fixtures(
        monkeypatch,
        sources=FIXTURE_SOURCES_HAPPY,
        feeds=_happy_feeds(),
    )

    result = run_daily(trigger="manual", db_path=db_path, dry_run=True)
    assert result.dry_run is True
    assert result.run_id is None
    assert calls["n"] == 0

    conn = _open_repo(db_path)
    try:
        news_count = conn.execute("SELECT COUNT(*) FROM news_items").fetchone()[0]
        runs_count = conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0]
        stats_count = conn.execute("SELECT COUNT(*) FROM source_run_stats").fetchone()[0]
        assert int(news_count) == 0
        assert int(runs_count) == 0
        assert int(stats_count) == 0
    finally:
        conn.close()
