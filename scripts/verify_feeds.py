#!/usr/bin/env python3
"""Verify that enabled RSS/Atom sources in config/sources.yaml respond.

Exit code is non-zero if any enabled source fails (HTTP error, empty/unparseable
feed, or network failure). Optional --sample prints a few titles (dry-run).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/verify_feeds.py` without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import feedparser
import httpx

from src.config_loader import enabled_sources
from src.ingest.rss_fetcher import DEFAULT_TIMEOUT_SECONDS, DEFAULT_USER_AGENT


def verify_source(
    source: dict,
    *,
    client: httpx.Client,
) -> tuple[bool, str]:
    """Return (ok, message) for one source. Never raises for expected failures."""
    source_id = source.get("id", "unknown")
    url = (source.get("url") or "").strip()
    if not url:
        return False, f"{source_id}: missing URL"

    try:
        response = client.get(url)
    except httpx.TimeoutException:
        return False, f"{source_id}: TIMEOUT {url}"
    except httpx.HTTPError as exc:
        return False, f"{source_id}: NETWORK ERROR {url} ({exc})"

    status = response.status_code
    if status >= 400:
        return False, f"{source_id}: HTTP {status} {url}"

    content_type = (response.headers.get("content-type") or "").lower()
    body_prefix = response.content.lstrip()[:200].lower()
    if b"<!doctype html" in body_prefix or b"<html" in body_prefix:
        return (
            False,
            f"{source_id}: HTTP {status}, HTML not feed "
            f"(possible bot challenge) {url} [content-type={content_type or 'unknown'}]",
        )

    parsed = feedparser.parse(response.content)
    count = len(parsed.entries)
    if count == 0:
        bozo = getattr(parsed, "bozo_exception", None)
        detail = f" (parse: {bozo})" if bozo else ""
        return False, f"{source_id}: HTTP {status}, 0 items{detail} {url}"

    return True, f"{source_id}: HTTP {status}, {count} items"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify enabled CI Intel feed URLs.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout seconds (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        metavar="N",
        help="If >0, also dry-run fetch and print N sample titles",
    )
    args = parser.parse_args(argv)

    sources = enabled_sources()
    if not sources:
        print("No enabled sources found in config/sources.yaml", file=sys.stderr)
        return 1

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    failures = 0
    print(f"Checking {len(sources)} enabled source(s)...\n")

    with httpx.Client(
        timeout=args.timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:
        for source in sources:
            ok, message = verify_source(source, client=client)
            prefix = "OK  " if ok else "FAIL"
            print(f"[{prefix}] {message}")
            if not ok:
                failures += 1

    print()
    if failures:
        print(f"Result: {failures}/{len(sources)} source(s) failed.")
    else:
        print(f"Result: all {len(sources)} enabled source(s) OK.")

    if args.sample > 0:
        print(f"\n--- Dry-run sample ({args.sample} titles) ---")
        from src.ingest.rss_fetcher import fetch_and_normalize

        result = fetch_and_normalize(timeout=args.timeout)
        for entry in result.entries[: args.sample]:
            print(f"  [{entry.source_id}] {entry.title}")
        if result.errors:
            print(f"(dry-run noted {len(result.errors)} source error(s))")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
