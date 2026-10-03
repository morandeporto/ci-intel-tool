"""Normalize raw feed entries into a stable internal shape.

Fetched web/RSS content is treated as untrusted text only - no LLM calls here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from src.process.dedupe import content_hash

DEFAULT_MAX_EXCERPT_CHARS = 4000


@dataclass(frozen=True)
class NormalizedEntry:
    """Canonical fields produced by ingestion before persistence / scoring."""

    title: str
    url: str
    published_at: str | None
    raw_excerpt: str
    source_id: str
    competitor: str
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strip_html(text: str) -> str:
    """Remove simple HTML tags, keep plain text for storage/prompts later."""
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return " ".join(without_tags.split())


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _parse_published_at(raw: dict[str, Any]) -> str | None:
    """Return ISO-8601 UTC string, or None when the feed omits a usable date."""
    # Prefer feedparser's struct_time fields when available.
    for key in ("published_parsed", "updated_parsed"):
        parsed = raw.get(key)
        if parsed:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except (TypeError, ValueError, OverflowError):
                pass

    for key in ("published", "updated"):
        value = raw.get(key)
        if not value or not isinstance(value, str):
            continue
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    return None


def _entry_url(raw: dict[str, Any]) -> str:
    link = raw.get("link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    links = raw.get("links") or []
    for item in links:
        if isinstance(item, dict) and item.get("href"):
            return str(item["href"]).strip()
    return ""


def _entry_excerpt(raw: dict[str, Any], max_chars: int) -> str:
    for key in ("summary", "description", "title"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate(_strip_html(value), max_chars)
    content = raw.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("value"), str):
            return _truncate(_strip_html(first["value"]), max_chars)
    return ""


def normalize_entry(
    raw: dict[str, Any],
    *,
    source_id: str,
    competitor: str,
    max_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> NormalizedEntry | None:
    """Map a feedparser-like entry dict into ``NormalizedEntry``.

    Returns None when title or URL is missing (unusable for the pipeline).
    """
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    title = " ".join(_strip_html(title).split())
    url = _entry_url(raw)
    if not url:
        return None

    return NormalizedEntry(
        title=title,
        url=url,
        published_at=_parse_published_at(raw),
        raw_excerpt=_entry_excerpt(raw, max_chars),
        source_id=source_id,
        competitor=competitor,
        content_hash=content_hash(title, url),
    )
