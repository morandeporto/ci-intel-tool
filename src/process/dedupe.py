"""Title normalization and content hashing for ingestion-time deduplication.

URL match catches identical posts; title hash catches near-duplicates across feeds.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Collapse casing/whitespace so equivalent headlines share one hash."""
    return _WHITESPACE_RE.sub(" ", title.strip().lower())


def content_hash(title: str, url: str | None = None) -> str:
    """SHA-256 of the normalized title (url reserved for callers; not hashed).

    Title-only hashing lets the same story from mirrored RSS feeds collide even
    when URLs differ. URL-based dedupe is handled separately in ``is_duplicate``.
    """
    # ``url`` is accepted for a stable call site with ingest; hashing stays
    # title-based so feed mirrors with different links still collide.
    _ = url
    digest = hashlib.sha256(normalize_title(title).encode("utf-8"))
    return digest.hexdigest()


def is_duplicate(
    url: str,
    content_hash: str,
    existing_urls: Collection[str],
    existing_hashes: Collection[str],
) -> bool:
    """True when the item was already stored by URL or by title hash."""
    return url in existing_urls or content_hash in existing_hashes
