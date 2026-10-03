"""Select which gate-passed entries proceed to the LLM.

Ranking:
  - official_competitor / emerging: by recency (newest first)
  - industry / community: by (strong keyword count, then recency)

Then keep top ``max_per_source`` per source, and fill kind-reserved slots with
round-robin across sources inside each kind bucket (up to max_items_per_run).
Unused reserved slots flow to other kinds. Cap-skipped items are NOT stored.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from src.ingest.normalize import NormalizedEntry
from src.process.freshness import parse_published_at
from src.process.relevance_gate import evaluate_gate


@dataclass(frozen=True)
class RankedEntry:
    """Entry with ranking metadata used for selection."""

    entry: NormalizedEntry
    kind: str
    source_id: str
    strong_match_count: int
    published_at: datetime | None


@dataclass
class SelectionResult:
    """Selected entries plus those skipped only because of the run cap."""

    selected: list[NormalizedEntry] = field(default_factory=list)
    cap_skipped: list[NormalizedEntry] = field(default_factory=list)
    per_source_selected: dict[str, int] = field(default_factory=dict)


def _kind_bucket(kind: str) -> str:
    if kind in ("industry", "community"):
        return "industry_community"
    return kind


def _recency_key(published: datetime | None) -> float:
    if published is None:
        return float("-inf")
    return published.timestamp()


def rank_entries_for_source(
    entries: Sequence[NormalizedEntry],
    *,
    kind: str,
    strong_counts: dict[str, int] | None = None,
) -> list[NormalizedEntry]:
    """Return entries sorted for this source kind (best first)."""
    strong_counts = strong_counts or {}

    def sort_key(entry: NormalizedEntry) -> tuple:
        published = parse_published_at(entry.published_at)
        recency = _recency_key(published)
        if kind in ("industry", "community"):
            return (-strong_counts.get(entry.url, 0), -recency)
        return (-recency,)

    return sorted(entries, key=sort_key)


def take_top_per_source(
    entries_by_source: dict[str, list[NormalizedEntry]],
    *,
    kind_by_source: dict[str, str],
    max_per_source: int,
    strong_counts: dict[str, int] | None = None,
) -> dict[str, list[NormalizedEntry]]:
    """Keep the top N ranked items per source."""
    if max_per_source <= 0:
        raise ValueError(f"max_per_source must be positive, got {max_per_source}")
    result: dict[str, list[NormalizedEntry]] = {}
    for source_id, entries in entries_by_source.items():
        kind = kind_by_source[source_id]
        ranked = rank_entries_for_source(
            entries, kind=kind, strong_counts=strong_counts
        )
        result[source_id] = ranked[:max_per_source]
    return result


def _round_robin_take(
    queues: dict[str, deque[NormalizedEntry]],
    *,
    limit: int,
) -> list[NormalizedEntry]:
    """Pull up to ``limit`` items, cycling sources in stable sorted order."""
    if limit <= 0:
        return []
    selected: list[NormalizedEntry] = []
    order = sorted(queues.keys())
    while len(selected) < limit:
        progressed = False
        for source_id in order:
            q = queues[source_id]
            if not q:
                continue
            selected.append(q.popleft())
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def balance_across_kinds(
    candidates_by_source: dict[str, list[NormalizedEntry]],
    *,
    kind_by_source: dict[str, str],
    reserved_slots: dict[str, int],
    max_items: int,
) -> SelectionResult:
    """Fill reserved kind slots with round-robin, unused slots spill over."""
    if max_items <= 0:
        return SelectionResult()

    # Build per-bucket queues of (source -> deque of ranked entries).
    bucket_queues: dict[str, dict[str, deque[NormalizedEntry]]] = defaultdict(dict)
    for source_id, entries in candidates_by_source.items():
        kind = kind_by_source[source_id]
        bucket = _kind_bucket(kind)
        bucket_queues[bucket][source_id] = deque(entries)

    bucket_order = ("official_competitor", "emerging", "industry_community")
    selected: list[NormalizedEntry] = []
    selected_urls: set[str] = set()

    # Pass 1: fill each kind's reserved minimum (round-robin within the kind).
    for bucket in bucket_order:
        want = int(reserved_slots.get(bucket, 0))
        room = max_items - len(selected)
        take_n = min(want, room)
        for entry in _round_robin_take(bucket_queues[bucket], limit=take_n):
            selected.append(entry)
            selected_urls.add(entry.url)

    # Pass 2: unused reserved slots (and any leftover room) flow to other kinds.
    spill_room = max_items - len(selected)
    if spill_room > 0:
        merged: dict[str, deque[NormalizedEntry]] = {}
        for bucket in bucket_order:
            for source_id, q in bucket_queues[bucket].items():
                if q:
                    merged[source_id] = q
        for entry in _round_robin_take(merged, limit=spill_room):
            if entry.url in selected_urls:
                continue
            selected.append(entry)
            selected_urls.add(entry.url)

    # Cap-skipped = still in queues after the global cap (not stored by caller).
    cap_skipped: list[NormalizedEntry] = []
    for bucket in bucket_order:
        for q in bucket_queues[bucket].values():
            while q:
                entry = q.popleft()
                if entry.url not in selected_urls:
                    cap_skipped.append(entry)

    per_source: dict[str, int] = defaultdict(int)
    for entry in selected:
        per_source[entry.source_id] += 1

    return SelectionResult(
        selected=selected,
        cap_skipped=cap_skipped,
        per_source_selected=dict(per_source),
    )


def select_for_llm(
    entries: Sequence[NormalizedEntry],
    *,
    source_meta: dict[str, dict[str, Any]],
    max_per_source: int,
    max_items: int,
    reserved_slots: dict[str, int],
    relevance_cfg: dict[str, Any] | None = None,
) -> SelectionResult:
    """Full selection: rank per source → top N → balance kinds → cap.

    ``source_meta`` maps source_id -> {kind, gate, ...}.
    Entries whose source_id is unknown are ignored.
    """
    by_source: dict[str, list[NormalizedEntry]] = defaultdict(list)
    kind_by_source: dict[str, str] = {}
    strong_counts: dict[str, int] = {}

    for entry in entries:
        meta = source_meta.get(entry.source_id)
        if not meta:
            continue
        kind = str(meta["kind"])
        kind_by_source[entry.source_id] = kind
        by_source[entry.source_id].append(entry)
        # Strong counts for industry/community ranking (gate already passed).
        decision = evaluate_gate(
            entry, gate=str(meta.get("gate") or "off"), relevance_cfg=relevance_cfg
        )
        strong_counts[entry.url] = decision.strong_match_count

    topped = take_top_per_source(
        by_source,
        kind_by_source=kind_by_source,
        max_per_source=max_per_source,
        strong_counts=strong_counts,
    )
    # Items beyond per-source top-N are also cap-like skips (not stored).
    beyond_per_source: list[NormalizedEntry] = []
    for source_id, entries_list in by_source.items():
        kind = kind_by_source[source_id]
        ranked = rank_entries_for_source(
            entries_list, kind=kind, strong_counts=strong_counts
        )
        beyond_per_source.extend(ranked[max_per_source:])

    result = balance_across_kinds(
        topped,
        kind_by_source=kind_by_source,
        reserved_slots=reserved_slots,
        max_items=max_items,
    )
    result.cap_skipped.extend(beyond_per_source)
    return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
