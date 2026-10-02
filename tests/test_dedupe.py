"""Unit tests for title normalization and content-hash deduplication."""

from __future__ import annotations

import hashlib

from src.process.dedupe import content_hash, is_duplicate, normalize_title


def test_normalize_title_lowercases_and_collapses_whitespace() -> None:
    assert normalize_title("  Hello   World\tNEWS ") == "hello world news"


def test_normalize_title_empty_after_strip() -> None:
    assert normalize_title("   ") == ""


def test_content_hash_is_sha256_of_normalized_title() -> None:
    title = "  JFrog   Artifactory Release "
    expected = hashlib.sha256(b"jfrog artifactory release").hexdigest()
    assert content_hash(title) == expected


def test_content_hash_ignores_url_argument() -> None:
    title = "Same Title"
    assert content_hash(title, url="https://a.example/1") == content_hash(
        title, url="https://b.example/2"
    )


def test_equivalent_titles_share_hash() -> None:
    assert content_hash("GitLab CI Update") == content_hash("  gitlab   ci  update ")


def test_different_titles_differ() -> None:
    assert content_hash("Alpha") != content_hash("Beta")


def test_is_duplicate_by_url() -> None:
    assert is_duplicate(
        "https://example.com/a",
        "hash-new",
        existing_urls={"https://example.com/a"},
        existing_hashes=set(),
    )


def test_is_duplicate_by_content_hash() -> None:
    digest = content_hash("Known Story")
    assert is_duplicate(
        "https://example.com/new",
        digest,
        existing_urls=set(),
        existing_hashes={digest},
    )


def test_is_not_duplicate_when_neither_matches() -> None:
    assert not is_duplicate(
        "https://example.com/new",
        content_hash("Brand New"),
        existing_urls={"https://example.com/old"},
        existing_hashes={content_hash("Other")},
    )
