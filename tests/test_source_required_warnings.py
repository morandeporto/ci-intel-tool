"""Non-required source failures are warnings; jfrog empty-202 is soft."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.ingest.rss_fetcher import (
    SourceFetchError,
    fetch_all_sources,
    source_is_required,
)


def test_community_required_false_in_config() -> None:
    from src.config_loader import load_sources

    community = [s for s in load_sources() if s.get("kind") == "community"]
    assert community
    assert all(source_is_required(s) is False for s in community)


def test_non_required_failure_is_warning_not_error(monkeypatch) -> None:
    source = {
        "id": "hn_sbom",
        "url": "https://hnrss.org/newest?q=SBOM",
        "kind": "community",
        "required": False,
        "competitor": None,
        "enabled": True,
        "gate": "strict",
    }

    def boom(src, **_k):
        return (
            [],
            SourceFetchError(
                "hn_sbom",
                source["url"],
                "HTTP 502",
                http_status="502",
                severity="error",
            ),
            "502",
            10,
        )

    monkeypatch.setattr("src.ingest.rss_fetcher.fetch_source", boom)
    result = fetch_all_sources([source], timeout=1.0)
    assert result.errors == []
    assert len(result.warnings) == 1
    assert result.source_stats[0].warning
    assert result.source_stats[0].error is None


def test_jfrog_empty_202_warning_after_retries(monkeypatch) -> None:
    source = {
        "id": "jfrog_blog",
        "url": "https://jfrog.com/blog/feed/",
        "kind": "official_competitor",
        "required": True,
        "competitor": "jfrog",
        "enabled": True,
        "gate": "off",
        "user_agent": "browser",
    }
    sleeps: list[float] = []

    class _Resp:
        status_code = 202
        content = b""
        headers = {"content-type": "text/html"}

        def raise_for_status(self) -> None:
            return None

    client = MagicMock()
    client.get.return_value = _Resp()

    def fake_client(*_a, **_k):
        return client

    monkeypatch.setattr("src.ingest.rss_fetcher.httpx.Client", fake_client)
    monkeypatch.setattr("src.ingest.rss_fetcher.time.sleep", sleeps.append)

    from src.ingest.rss_fetcher import fetch_source

    entries, err, status, _ms = fetch_source(source, timeout=1.0)
    assert entries == []
    assert status == "202"
    assert err is not None
    assert err.severity == "warning"
    assert len(sleeps) == 2
    assert sleeps[0] == 5.0
    assert sleeps[1] == 15.0
