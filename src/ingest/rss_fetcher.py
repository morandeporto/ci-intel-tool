"""Fetch RSS/Atom feeds for configured competitors.

Per-source errors are collected and returned, a single bad feed never aborts
the whole run. Fetches run concurrently with a per-source timeout. Content is
treated as untrusted text (no LLM in this module).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx

from src.config_loader import enabled_sources, load_model_config, source_competitor_tag
from src.ingest.normalize import (
    DEFAULT_MAX_EXCERPT_CHARS,
    NormalizedEntry,
    normalize_entry,
)

logger = logging.getLogger(__name__)

# Honest identifiable UA - prefer this. Browser UA only for sources that need it.
HONEST_USER_AGENT = (
    "ci-intel-tool/1.0 (+https://github.com/morandeporto/ci-intel-tool; "
    "take-home assignment)"
)
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Backward-compatible alias used by verify_feeds / diagnose scripts.
DEFAULT_USER_AGENT = HONEST_USER_AGENT
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_FETCH_CONCURRENCY = 8
# hnrss.org occasionally returns 502/503, a short backoff usually recovers.
HN_5XX_MAX_ATTEMPTS = 3
HN_5XX_BASE_SECONDS = 1.0
# jfrog_blog intermittently returns HTTP 202 with an empty body.
JFROG_EMPTY_202_ATTEMPTS = 3
JFROG_EMPTY_202_BACKOFF_SECONDS = (5.0, 15.0)

ACCEPT_HEADERS = (
    "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
)


@dataclass
class SourceFetchError:
    source_id: str
    url: str
    message: str
    http_status: str | None = None
    # warning = telemetry only, must not flip pipeline status to partial
    severity: str = "error"


@dataclass
class SourceFetchStat:
    """Per-source fetch telemetry (filled further by the pipeline)."""

    source_id: str
    http_status: str | None = None
    fetched: int = 0
    error: str | None = None
    warning: str | None = None
    duration_ms: int | None = None


@dataclass
class FetchResult:
    """Raw feedparser entry dicts plus per-source failures."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[SourceFetchError] = field(default_factory=list)
    warnings: list[SourceFetchError] = field(default_factory=list)
    source_stats: list[SourceFetchStat] = field(default_factory=list)


@dataclass
class NormalizedFetchResult:
    entries: list[NormalizedEntry] = field(default_factory=list)
    errors: list[SourceFetchError] = field(default_factory=list)
    warnings: list[SourceFetchError] = field(default_factory=list)
    source_stats: list[SourceFetchStat] = field(default_factory=list)


def source_is_required(source: dict[str, Any]) -> bool:
    """Sources default to required=true, community YAML sets required: false."""
    if "required" in source:
        return bool(source.get("required"))
    return True


def _fetch_timeout_seconds(timeout: float | None) -> float:
    if timeout is not None:
        return float(timeout)
    try:
        cfg = load_model_config()
        return float(cfg.get("fetch_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except Exception:
        return DEFAULT_TIMEOUT_SECONDS


def _timeout_seconds(timeout: float | None) -> float:
    """Alias kept for callers/scripts that used the old name."""
    return _fetch_timeout_seconds(timeout)


def _fetch_concurrency() -> int:
    try:
        cfg = load_model_config()
        return max(1, int(cfg.get("fetch_concurrency", DEFAULT_FETCH_CONCURRENCY)))
    except Exception:
        return DEFAULT_FETCH_CONCURRENCY


def _max_excerpt_chars() -> int:
    try:
        cfg = load_model_config()
        return int(cfg.get("max_excerpt_chars", DEFAULT_MAX_EXCERPT_CHARS))
    except Exception:
        return DEFAULT_MAX_EXCERPT_CHARS


def _window_hours_default() -> int:
    try:
        return int(load_model_config().get("window_hours", 48))
    except Exception:
        return 48


def _user_agent_for_source(source: dict[str, Any]) -> str:
    mode = str(source.get("user_agent") or "honest").strip().lower()
    if mode == "browser":
        return BROWSER_USER_AGENT
    return HONEST_USER_AGENT


def _raw_published_utc(raw: dict[str, Any]) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = raw.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _entry_in_window(
    raw: dict[str, Any],
    *,
    window_hours: int,
    now: datetime,
) -> bool:
    """True if published_at is within (now - window, now]. Missing/future → False."""
    published = _raw_published_utc(raw)
    if published is None:
        return False
    if published > now:
        return False
    return published >= now - timedelta(hours=window_hours)


def _should_retry_5xx(source_id: str) -> bool:
    """HN keyword feeds via hnrss.org are prone to intermittent 502/503."""
    return source_id.startswith("hn_")


def _http_get_with_optional_5xx_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    timeout_s: float,
    source_id: str,
    sleep_fn=time.sleep,
) -> httpx.Response:
    """GET with exponential backoff on 5xx for hn_* feeds only."""
    attempts = HN_5XX_MAX_ATTEMPTS if _should_retry_5xx(source_id) else 1
    last_response: httpx.Response | None = None
    for attempt in range(attempts):
        response = client.get(url, headers=headers, timeout=timeout_s)
        last_response = response
        if response.status_code < 500 or attempt >= attempts - 1:
            return response
        delay = HN_5XX_BASE_SECONDS * (2**attempt)
        logger.warning(
            "HTTP %s for %s (attempt %s/%s), retrying in %.1fs",
            response.status_code,
            source_id,
            attempt + 1,
            attempts,
            delay,
        )
        sleep_fn(delay)
    assert last_response is not None
    return last_response


def fetch_source(
    source: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    timeout: float | None = None,
    window_hours: int | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], SourceFetchError | None, str | None, int]:
    """GET one feed URL and parse with feedparser.

    Returns (raw_entries_in_window, error, http_status, duration_ms).
    Only entries inside the freshness window are returned (avoids normalizing
    huge historical archives such as snyk_blog).
    """
    source_id = str(source.get("id", "unknown"))
    url = str(source.get("url", "")).strip()
    started = time.perf_counter()
    if not url:
        return (
            [],
            SourceFetchError(source_id, url, "Source has no URL"),
            None,
            0,
        )

    timeout_s = _fetch_timeout_seconds(timeout)
    window = int(window_hours if window_hours is not None else _window_hours_default())
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    headers = {
        "User-Agent": _user_agent_for_source(source),
        "Accept": ACCEPT_HEADERS,
    }
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=timeout_s, headers=headers, follow_redirects=True
        )

    http_status: str | None = None
    try:
        assert client is not None
        # Per-request timeout so a shared client cannot stretch beyond config.
        response = _http_get_with_optional_5xx_retry(
            client,
            url,
            headers=headers,
            timeout_s=timeout_s,
            source_id=source_id,
        )
        # jfrog_blog: empty HTTP 202 is retryable (CDN/WAF flake), not a hard error.
        if source_id == "jfrog_blog":
            for attempt in range(JFROG_EMPTY_202_ATTEMPTS):
                http_status = str(response.status_code)
                body_len = len(response.content or b"")
                if response.status_code != 202 or body_len > 0:
                    break
                if attempt >= JFROG_EMPTY_202_ATTEMPTS - 1:
                    break
                delay = JFROG_EMPTY_202_BACKOFF_SECONDS[
                    min(attempt, len(JFROG_EMPTY_202_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "jfrog_blog empty HTTP 202 (attempt %s/%s), retrying in %.0fs",
                    attempt + 1,
                    JFROG_EMPTY_202_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                response = _http_get_with_optional_5xx_retry(
                    client,
                    url,
                    headers=headers,
                    timeout_s=timeout_s,
                    source_id=source_id,
                )
        http_status = str(response.status_code)
        # Persistent empty 202 → warning (not error), return no entries.
        if (
            source_id == "jfrog_blog"
            and response.status_code == 202
            and len(response.content or b"") == 0
        ):
            ms = int((time.perf_counter() - started) * 1000)
            return (
                [],
                SourceFetchError(
                    source_id,
                    url,
                    "Empty HTTP 202 body after retries (intermittent CDN/WAF)",
                    http_status="202",
                    severity="warning",
                ),
                "202",
                ms,
            )
        response.raise_for_status()
    except httpx.TimeoutException:
        ms = int((time.perf_counter() - started) * 1000)
        return (
            [],
            SourceFetchError(
                source_id, url, f"Request timed out for {url}", http_status="timeout"
            ),
            "timeout",
            ms,
        )
    except httpx.HTTPStatusError as exc:
        status = str(exc.response.status_code)
        ms = int((time.perf_counter() - started) * 1000)
        return (
            [],
            SourceFetchError(
                source_id, url, f"HTTP {status} for {url}", http_status=status
            ),
            status,
            ms,
        )
    except httpx.HTTPError as exc:
        ms = int((time.perf_counter() - started) * 1000)
        return (
            [],
            SourceFetchError(
                source_id,
                url,
                f"Network error for {url}: {exc}",
                http_status="network_error",
            ),
            "network_error",
            ms,
        )
    finally:
        if owns_client and client is not None:
            try:
                client.close()
            except Exception:
                pass

    # Untrusted body - store/parse only, never execute or trust as instructions.
    content_type = (response.headers.get("content-type") or "").lower()
    body_prefix = response.content.lstrip()[:200].lower()
    if b"<!doctype html" in body_prefix or b"<html" in body_prefix:
        ms = int((time.perf_counter() - started) * 1000)
        return (
            [],
            SourceFetchError(
                source_id,
                url,
                f"Expected RSS/Atom but got HTML (possible bot challenge) for {url} "
                f"[content-type={content_type or 'unknown'}]",
                http_status=http_status,
            ),
            http_status,
            ms,
        )

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        detail = getattr(parsed, "bozo_exception", None)
        msg = f"Feed parse failed for {url}"
        if detail:
            msg = f"{msg}: {detail}"
        ms = int((time.perf_counter() - started) * 1000)
        return (
            [],
            SourceFetchError(source_id, url, msg, http_status=http_status),
            http_status,
            ms,
        )

    competitor = source_competitor_tag(source)
    raw_entries: list[dict[str, Any]] = []
    for entry in parsed.entries:
        item = dict(entry)
        if not _entry_in_window(item, window_hours=window, now=now_utc):
            continue
        item["_source_id"] = source_id
        item["_competitor"] = competitor
        item["_source_url"] = url
        raw_entries.append(item)

    ms = int((time.perf_counter() - started) * 1000)
    return raw_entries, None, http_status, ms


def _fetch_one_isolated(
    source: dict[str, Any],
    *,
    timeout: float | None,
    window_hours: int | None,
) -> tuple[list[dict[str, Any]], SourceFetchError | None, SourceFetchStat]:
    """Never raises - wraps fetch_source for the thread pool."""
    source_id = str(source.get("id", "unknown"))
    try:
        entries, error, http_status, duration_ms = fetch_source(
            source, timeout=timeout, window_hours=window_hours
        )
    except Exception as exc:  # noqa: BLE001
        err = SourceFetchError(
            source_id,
            str(source.get("url", "")),
            f"Unexpected error: {exc}",
            http_status="exception",
        )
        stat = SourceFetchStat(
            source_id=source_id,
            http_status="exception",
            fetched=0,
            error=err.message,
            duration_ms=None,
        )
        return [], err, stat

    is_warning = bool(error and error.severity == "warning")
    stat = SourceFetchStat(
        source_id=source_id,
        http_status=http_status or (error.http_status if error else None),
        fetched=len(entries),
        error=None if is_warning else (error.message if error else None),
        warning=error.message if is_warning else None,
        duration_ms=duration_ms,
    )
    return entries, error, stat


def fetch_all_sources(
    sources: list[dict[str, Any]] | None = None,
    *,
    timeout: float | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    window_hours: int | None = None,
) -> FetchResult:
    """Fetch every enabled source concurrently, never raise for individual failures.

    ``user_agent`` is ignored for per-source selection (kept for API compatibility);
    each source uses honest or browser UA from its YAML ``user_agent`` field.
    Non-required source failures become warnings (do not affect run status).
    """
    del user_agent  # per-source UA from YAML
    sources = sources if sources is not None else enabled_sources()
    result = FetchResult()
    if not sources:
        return result

    workers = min(_fetch_concurrency(), len(sources))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _fetch_one_isolated,
                source,
                timeout=timeout,
                window_hours=window_hours,
            ): source
            for source in sources
        }
        for fut in as_completed(futures):
            source = futures[fut]
            source_id = str(source.get("id", "unknown"))
            required = source_is_required(source)
            try:
                entries, error, stat = fut.result()
            except Exception as exc:  # noqa: BLE001 - isolate pool failures
                err = SourceFetchError(
                    source_id,
                    str(source.get("url", "")),
                    f"Worker failed: {exc}",
                    http_status="exception",
                    severity="warning" if not required else "error",
                )
                if err.severity == "warning":
                    result.warnings.append(err)
                    result.source_stats.append(
                        SourceFetchStat(
                            source_id=source_id,
                            http_status="exception",
                            fetched=0,
                            warning=err.message,
                            duration_ms=None,
                        )
                    )
                else:
                    result.errors.append(err)
                    result.source_stats.append(
                        SourceFetchStat(
                            source_id=source_id,
                            http_status="exception",
                            fetched=0,
                            error=err.message,
                            duration_ms=None,
                        )
                    )
                continue
            if error:
                # Non-required sources: demote hard errors to warnings.
                if error.severity != "warning" and not required:
                    error = SourceFetchError(
                        error.source_id,
                        error.url,
                        error.message,
                        http_status=error.http_status,
                        severity="warning",
                    )
                    stat = SourceFetchStat(
                        source_id=stat.source_id,
                        http_status=stat.http_status,
                        fetched=stat.fetched,
                        error=None,
                        warning=error.message,
                        duration_ms=stat.duration_ms,
                    )
                if error.severity == "warning":
                    result.warnings.append(error)
                else:
                    result.errors.append(error)
            result.entries.extend(entries)
            result.source_stats.append(stat)

    # Stable order for telemetry readability.
    result.source_stats.sort(key=lambda s: s.source_id)
    return result


def fetch_and_normalize(
    sources: list[dict[str, Any]] | None = None,
    *,
    timeout: float | None = None,
    max_chars: int | None = None,
    window_hours: int | None = None,
) -> NormalizedFetchResult:
    """Fetch feeds and map in-window entries into ``NormalizedEntry`` objects."""
    raw = fetch_all_sources(sources, timeout=timeout, window_hours=window_hours)
    limit = max_chars if max_chars is not None else _max_excerpt_chars()
    normalized: list[NormalizedEntry] = []
    for entry in raw.entries:
        source_id = str(entry.get("_source_id") or "")
        competitor = str(entry.get("_competitor") or "")
        item = normalize_entry(
            entry,
            source_id=source_id,
            competitor=competitor,
            max_chars=limit,
        )
        if item is not None:
            normalized.append(item)

    by_source: dict[str, int] = {}
    for item in normalized:
        by_source[item.source_id] = by_source.get(item.source_id, 0) + 1
    stats = [
        SourceFetchStat(
            source_id=stat.source_id,
            http_status=stat.http_status,
            fetched=(
                0
                if (stat.error or stat.warning)
                else by_source.get(stat.source_id, 0)
            ),
            error=stat.error,
            warning=stat.warning,
            duration_ms=stat.duration_ms,
        )
        for stat in raw.source_stats
    ]

    return NormalizedFetchResult(
        entries=normalized,
        errors=list(raw.errors),
        warnings=list(raw.warnings),
        source_stats=stats,
    )


def _dry_run(sample: int = 5, timeout: float | None = None) -> int:
    """Print a few sample titles without writing to the database."""
    result = fetch_and_normalize(timeout=timeout)
    print(f"Fetched {len(result.entries)} normalized entries from enabled sources.")
    if result.errors:
        print(f"Source errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  - {err.source_id}: {err.message}")
    print(f"\nSample titles (up to {sample}):")
    for entry in result.entries[:sample]:
        print(f"  [{entry.competitor}/{entry.source_id}] {entry.title}")
        print(f"    {entry.url}")
    return 1 if result.errors and not result.entries else 0


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Dry-run RSS fetch (no DB writes).")
    parser.add_argument("--sample", type=int, default=5, help="Titles to print")
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout seconds")
    args = parser.parse_args()
    sys.exit(_dry_run(sample=args.sample, timeout=args.timeout))
