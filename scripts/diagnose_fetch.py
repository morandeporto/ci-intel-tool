#!/usr/bin/env python3
"""Read-only fetch diagnosis: last-24h coverage, keyword gate, cap simulations.

No DB writes, no LLM calls, never touches Turso (local --db SQLite only).
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import httpx

# Allow `python scripts/diagnose_fetch.py` from repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import enabled_sources, load_model_config  # noqa: E402
from src.db.connection import get_connection  # noqa: E402
from src.db.repository import Repository  # noqa: E402
from src.ingest.normalize import NormalizedEntry, normalize_entry  # noqa: E402
from src.ingest.rss_fetcher import DEFAULT_USER_AGENT  # noqa: E402
from src.process.dedupe import is_duplicate  # noqa: E402

KEYWORDS = [
    "supply chain",
    "artifact",
    "registry",
    "package",
    "npm",
    "pypi",
    "maven",
    "container",
    "docker",
    "sca",
    "sbom",
    "vulnerability",
    "cve",
    "devsecops",
    "ci/cd",
    "mcp",
    "malicious package",
    "sonatype",
    "nexus",
    "github",
    "gitlab",
    "snyk",
    "cloudsmith",
    "harness",
    "jfrog",
    "artifactory",
    "xray",
]


@dataclass
class SourcedItem:
    entry: NormalizedEntry
    source_id: str
    competitor: str
    category: str
    kind: str  # official | community_industry
    match_count: int = 0
    passes_gate: bool = False


@dataclass
class SourceReport:
    source_id: str
    competitor: str
    category: str
    kind: str
    fetched_total: int = 0
    missing_date: int = 0
    in_24h: int = 0
    new_24h: int = 0
    newest_published: str | None = None
    http_status: str = ""
    items_24h: list[SourcedItem] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _source_kind(competitor: str) -> str:
    return "community_industry" if competitor == "industry" else "official"


def _keyword_matches(title: str, summary: str) -> int:
    text = f"{title} {summary}".lower()
    count = 0
    for kw in KEYWORDS:
        # Word-ish match, allow CI/CD and multi-word phrases.
        pattern = re.escape(kw.lower())
        if re.search(pattern, text):
            count += 1
    return count


def _passes_gate(kind: str, match_count: int) -> bool:
    threshold = 1 if kind == "official" else 2
    return match_count >= threshold


def _fetch_one(
    source: dict[str, Any],
    *,
    client: httpx.Client,
    max_chars: int,
) -> tuple[list[NormalizedEntry], str]:
    """Return (normalized entries, http_status_or_error). Never raises."""
    source_id = str(source.get("id", "unknown"))
    url = str(source.get("url", "")).strip()
    competitor = str(source.get("competitor") or "")
    if not url:
        return [], "error: no URL"

    try:
        response = client.get(url)
        status = str(response.status_code)
        response.raise_for_status()
    except httpx.TimeoutException:
        return [], "error: timeout"
    except httpx.HTTPStatusError as exc:
        return [], f"HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        return [], f"error: network ({exc})"

    body_prefix = response.content.lstrip()[:200].lower()
    if b"<!doctype html" in body_prefix or b"<html" in body_prefix:
        return [], f"HTTP {status} (HTML body, not feed)"

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        detail = getattr(parsed, "bozo_exception", None)
        return [], f"HTTP {status} (parse failed: {detail})"

    entries: list[NormalizedEntry] = []
    for raw in parsed.entries:
        item = dict(raw)
        normalized = normalize_entry(
            item,
            source_id=source_id,
            competitor=competitor,
            max_chars=max_chars,
        )
        if normalized is not None:
            entries.append(normalized)
    return entries, f"HTTP {status}"


def _load_dedupe_sets(db_path: Path) -> tuple[set[str], set[str]]:
    """Read-only local SQLite - never call get_connection() without a path."""
    if not db_path.exists():
        return set(), set()
    conn = get_connection(db_path)
    try:
        repo = Repository(conn)
        return repo.existing_urls(), repo.existing_content_hashes()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _round_robin(
    items: list[SourcedItem],
    *,
    max_total: int | None,
    max_per_source: int,
) -> list[SourcedItem]:
    by_source: dict[str, list[SourcedItem]] = defaultdict(list)
    for item in items:
        by_source[item.source_id].append(item)

    # Preserve YAML / first-seen source order.
    source_order: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.source_id not in seen:
            seen.add(item.source_id)
            source_order.append(item.source_id)

    pointers = {sid: 0 for sid in source_order}
    taken_per_source: dict[str, int] = defaultdict(int)
    selected: list[SourcedItem] = []

    progressed = True
    while progressed:
        progressed = False
        for sid in source_order:
            if max_total is not None and len(selected) >= max_total:
                return selected
            if taken_per_source[sid] >= max_per_source:
                continue
            idx = pointers[sid]
            bucket = by_source[sid]
            if idx >= len(bucket):
                continue
            selected.append(bucket[idx])
            pointers[sid] = idx + 1
            taken_per_source[sid] += 1
            progressed = True
    return selected


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in str_rows:
        print(fmt.format(*row))


def _cap_summary(label: str, selected: list[SourcedItem]) -> None:
    by_comp: dict[str, int] = defaultdict(int)
    sources = {s.source_id for s in selected}
    for s in selected:
        by_comp[s.competitor] += 1
    print(f"\n### {label}")
    print(f"total_selected={len(selected)}  sources_represented={len(sources)}  "
          f"estimated_gemini_calls={len(selected)}")
    if by_comp:
        rows = [[c, by_comp[c]] for c in sorted(by_comp, key=lambda k: (-by_comp[k], k))]
        _print_table(["competitor", "count"], rows)
    else:
        print("(none selected)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only 24h fetch / gate / cap diagnosis (local SQLite only)."
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Local SQLite path for dedupe lookups (never Turso).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for title samples (default 42).",
    )
    args = parser.parse_args(argv)

    db_path = args.db.expanduser().resolve()
    # Guardrail: never open Turso from this script.
    print(f"db={db_path} (local SQLite only, Turso disabled for this script)")
    print(f"as_of={_utc_now().replace(microsecond=0).isoformat()}")

    model_cfg = load_model_config()
    max_chars = int(model_cfg.get("max_excerpt_chars", 4000))
    timeout_s = float(model_cfg.get("request_timeout_seconds", 30))
    max_per_run = int(model_cfg["max_items_per_run"])

    sources = enabled_sources()
    existing_urls, existing_hashes = _load_dedupe_sets(db_path)
    # Global accumulating sets (YAML order) - matches pipeline dedupe behavior.
    urls = set(existing_urls)
    hashes = set(existing_hashes)
    cutoff = _utc_now() - timedelta(hours=24)

    reports: list[SourceReport] = []
    all_24h: list[SourcedItem] = []
    # Baseline (a): all newly fetched items in YAML order (no 24h filter, no gate)
    baseline_new: list[SourcedItem] = []

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": (
            "application/rss+xml, application/atom+xml, "
            "application/xml, text/xml, */*"
        ),
    }

    with httpx.Client(
        timeout=timeout_s, headers=headers, follow_redirects=True
    ) as client:
        for source in sources:
            source_id = str(source["id"])
            competitor = str(source.get("competitor") or "")
            category = str(source.get("category") or "")
            kind = _source_kind(competitor)
            report = SourceReport(
                source_id=source_id,
                competitor=competitor,
                category=category,
                kind=kind,
            )
            entries, status = _fetch_one(
                source, client=client, max_chars=max_chars
            )
            report.http_status = status
            report.fetched_total = len(entries)

            newest: datetime | None = None

            for entry in entries:
                pub = _parse_iso(entry.published_at)
                if pub is None:
                    report.missing_date += 1
                    # Still track baseline-new without date filter.
                    if not is_duplicate(entry.url, entry.content_hash, urls, hashes):
                        item = SourcedItem(
                            entry=entry,
                            source_id=source_id,
                            competitor=competitor,
                            category=category,
                            kind=kind,
                        )
                        baseline_new.append(item)
                        urls.add(entry.url)
                        hashes.add(entry.content_hash)
                    continue

                if newest is None or pub > newest:
                    newest = pub

                matches = _keyword_matches(entry.title, entry.raw_excerpt)
                passes = _passes_gate(kind, matches)
                item = SourcedItem(
                    entry=entry,
                    source_id=source_id,
                    competitor=competitor,
                    category=category,
                    kind=kind,
                    match_count=matches,
                    passes_gate=passes,
                )

                is_new = not is_duplicate(
                    entry.url, entry.content_hash, urls, hashes
                )
                if is_new:
                    baseline_new.append(item)
                    urls.add(entry.url)
                    hashes.add(entry.content_hash)

                if pub < cutoff:
                    continue

                report.in_24h += 1
                report.items_24h.append(item)
                all_24h.append(item)
                if is_new:
                    report.new_24h += 1

            report.newest_published = newest.isoformat() if newest else None
            reports.append(report)

    # --- 1. Per source -------------------------------------------------
    print("\n== 1. Per source (enabled) ==")
    rows = []
    for r in reports:
        rows.append(
            [
                r.source_id,
                r.competitor,
                r.fetched_total,
                r.in_24h,
                r.new_24h,
                r.missing_date,
                r.newest_published or "-",
                r.http_status,
            ]
        )
    _print_table(
        [
            "source_id",
            "competitor",
            "fetched",
            "in_24h",
            "new_24h",
            "missing_date",
            "newest_published",
            "http/error",
        ],
        rows,
    )

    # --- 2. By competitor / kind (24h only) ----------------------------
    print("\n== 2. By competitor tag (24h only) ==")
    by_comp: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for item in all_24h:
        by_comp[item.competitor] += 1
        by_kind[item.kind] += 1
    _print_table(
        ["competitor", "count_24h"],
        [[c, by_comp[c]] for c in sorted(by_comp, key=lambda k: (-by_comp[k], k))]
        or [["(none)", 0]],
    )
    print("\nOfficial vs community/industry (24h only):")
    _print_table(
        ["kind", "count_24h"],
        [[k, by_kind[k]] for k in sorted(by_kind)] or [["(none)", 0]],
    )

    # --- 3. Totals -----------------------------------------------------
    sources_with_24h = sum(1 for r in reports if r.in_24h > 0)
    print("\n== 3. Totals (24h) ==")
    print(f"total_24h_items={len(all_24h)}")
    print(f"sources_with_at_least_one={sources_with_24h} / {len(reports)}")
    print(
        f"missing_or_unparseable_dates_total="
        f"{sum(r.missing_date for r in reports)} (excluded from 24h numbers)"
    )

    # --- 4. Relevance gate ---------------------------------------------
    print("\n== 4. Relevance gate (keyword, no LLM, 24h only) ==")
    print(
        "threshold: official >= 1 match, community/industry >= 2 matches"
    )
    gate_rows = []
    pass_by_comp: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    passed_items = [i for i in all_24h if i.passes_gate]
    filtered_items = [i for i in all_24h if not i.passes_gate]

    for r in reports:
        p = sum(1 for i in r.items_24h if i.passes_gate)
        f = sum(1 for i in r.items_24h if not i.passes_gate)
        gate_rows.append([r.source_id, r.competitor, r.kind, r.in_24h, p, f])
        pass_by_comp[r.competitor][0] += p
        pass_by_comp[r.competitor][1] += f

    _print_table(
        ["source_id", "competitor", "kind", "in_24h", "pass", "filtered"],
        gate_rows,
    )
    print("\nPer competitor tag (24h gate):")
    _print_table(
        ["competitor", "pass", "filtered"],
        [
            [c, pass_by_comp[c][0], pass_by_comp[c][1]]
            for c in sorted(pass_by_comp, key=lambda k: (-sum(pass_by_comp[k]), k))
            if pass_by_comp[c][0] or pass_by_comp[c][1]
        ]
        or [["(none)", 0, 0]],
    )

    rng = random.Random(args.seed)
    pass_sample = passed_items[:]
    filt_sample = filtered_items[:]
    rng.shuffle(pass_sample)
    rng.shuffle(filt_sample)
    pass_sample = pass_sample[:15]
    filt_sample = filt_sample[:15]

    print(f"\nSample PASSED titles ({len(pass_sample)} of {len(passed_items)}):")
    for i, item in enumerate(pass_sample, 1):
        print(
            f"  {i:2}. [{item.competitor}/{item.source_id} matches={item.match_count}] "
            f"{item.entry.title}"
        )
    print(f"\nSample FILTERED titles ({len(filt_sample)} of {len(filtered_items)}):")
    for i, item in enumerate(filt_sample, 1):
        print(
            f"  {i:2}. [{item.competitor}/{item.source_id} matches={item.match_count}] "
            f"{item.entry.title}"
        )

    # --- 5. Cap simulations --------------------------------------------
    print("\n== 5. Cap simulations ==")
    print(
        f"baseline uses ALL new items (any date), YAML order, [:{max_per_run}], no gate"
    )
    print("b/c use 24h items that PASS the gate")

    # a) Current behavior on baseline_new (already YAML order)
    selected_a = baseline_new[:max_per_run]
    _cap_summary(
        f"a. Current: YAML order, [:{max_per_run}], no gate (any-date new)",
        selected_a,
    )

    # b) Gate + round-robin, max 20, max 3/source
    selected_b = _round_robin(passed_items, max_total=20, max_per_source=3)
    _cap_summary(
        "b. Gate + round-robin, max 20 total, max 3/source (24h pass)",
        selected_b,
    )

    # c) Gate + no total cap, max 3/source
    selected_c = _round_robin(passed_items, max_total=None, max_per_source=3)
    _cap_summary(
        "c. Gate + no total cap, max 3/source (24h pass)",
        selected_c,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
