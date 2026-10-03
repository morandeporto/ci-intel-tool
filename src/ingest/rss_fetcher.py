"""Fetch RSS/Atom feeds for configured competitors.

Per-source errors are collected and returned; a single bad feed never aborts
the whole run. Content is treated as untrusted text (no LLM in this module).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import feedparser
import httpx

from src.config_loader import enabled_sources, load_model_config, source_competitor_tag
from src.ingest.normalize import (
    DEFAULT_MAX_EXCERPT_CHARS,
    NormalizedEntry,
    normalize_entry,
)

DEFAULT_USER_AGENT = (
    "ci-intel-tool/1.0 (+https://github.com/jfrog-ci-intel; take-home assignment)"
)
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class SourceFetchError:
    source_id: str
    url: str
    message: str


@dataclass
class FetchResult:
    """Raw feedparser entry dicts plus per-source failures."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[SourceFetchError] = field(default_factory=list)


@dataclass
class NormalizedFetchResult:
    entries: list[NormalizedEntry] = field(default_factory=list)
    errors: list[SourceFetchError] = field(default_factory=list)


def _timeout_seconds(timeout: float | None) -> float:
    if timeout is not None:
        return float(timeout)
    try:
        cfg = load_model_config()
        return float(cfg.get("request_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except Exception:
        return DEFAULT_TIMEOUT_SECONDS


def _max_excerpt_chars() -> int:
    try:
        cfg = load_model_config()
        return int(cfg.get("max_excerpt_chars", DEFAULT_MAX_EXCERPT_CHARS))
    except Exception:
        return DEFAULT_MAX_EXCERPT_CHARS


def fetch_source(
    source: dict[str, Any],
    *,
    client: httpx.Client,
) -> tuple[list[dict[str, Any]], SourceFetchError | None]:
    """GET one feed URL and parse with feedparser.

    Returns (raw_entries, error). On failure, raw_entries is empty and error
    is set — callers should continue with other sources.
    """
    source_id = str(source.get("id", "unknown"))
    url = str(source.get("url", "")).strip()
    if not url:
        return [], SourceFetchError(source_id, url, "Source has no URL")

    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.TimeoutException:
        return [], SourceFetchError(source_id, url, f"Request timed out for {url}")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        return [], SourceFetchError(source_id, url, f"HTTP {status} for {url}")
    except httpx.HTTPError as exc:
        return [], SourceFetchError(source_id, url, f"Network error for {url}: {exc}")

    # Untrusted body — store/parse only; never execute or trust as instructions.
    content_type = (response.headers.get("content-type") or "").lower()
    body_prefix = response.content.lstrip()[:200].lower()
    if b"<!doctype html" in body_prefix or b"<html" in body_prefix:
        return [], SourceFetchError(
            source_id,
            url,
            f"Expected RSS/Atom but got HTML (possible bot challenge) for {url} "
            f"[content-type={content_type or 'unknown'}]",
        )

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        detail = getattr(parsed, "bozo_exception", None)
        msg = f"Feed parse failed for {url}"
        if detail:
            msg = f"{msg}: {detail}"
        return [], SourceFetchError(source_id, url, msg)

    raw_entries: list[dict[str, Any]] = []
    for entry in parsed.entries:
        # Attach provenance so normalize / dry-run can attribute items.
        item = dict(entry)
        item["_source_id"] = source_id
        item["_competitor"] = source_competitor_tag(source)
        item["_source_url"] = url
        raw_entries.append(item)
    return raw_entries, None


def fetch_all_sources(
    sources: list[dict[str, Any]] | None = None,
    *,
    timeout: float | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchResult:
    """Fetch every enabled source; never raise for individual feed failures."""
    sources = sources if sources is not None else enabled_sources()
    result = FetchResult()
    timeout_s = _timeout_seconds(timeout)
    headers = {"User-Agent": user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}

    with httpx.Client(timeout=timeout_s, headers=headers, follow_redirects=True) as client:
        for source in sources:
            try:
                entries, error = fetch_source(source, client=client)
            except Exception as exc:  # noqa: BLE001 — isolate unexpected per-source crashes
                source_id = str(source.get("id", "unknown"))
                url = str(source.get("url", ""))
                result.errors.append(
                    SourceFetchError(source_id, url, f"Unexpected error: {exc}")
                )
                continue
            if error:
                result.errors.append(error)
            result.entries.extend(entries)
    return result


def fetch_and_normalize(
    sources: list[dict[str, Any]] | None = None,
    *,
    timeout: float | None = None,
    max_chars: int | None = None,
) -> NormalizedFetchResult:
    """Fetch feeds and map usable entries into ``NormalizedEntry`` objects."""
    raw = fetch_all_sources(sources, timeout=timeout)
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
    return NormalizedFetchResult(entries=normalized, errors=list(raw.errors))


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
