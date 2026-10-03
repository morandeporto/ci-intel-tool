"""hn_* feed 5xx retry (mocked HTTP - no live network)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.ingest import rss_fetcher


class _Resp:
    def __init__(self, status_code: int, content: bytes = b"<rss/>") -> None:
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "application/rss+xml"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://hnrss.org/x"),
                response=httpx.Response(self.status_code),
            )


def test_hn_5xx_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(502)
        return _Resp(200, b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>t</title>
        <item><title>SBOM news</title><link>https://ex.com/1</link>
        <pubDate>Fri, 03 Oct 2026 12:00:00 GMT</pubDate></item>
        </channel></rss>""")

    client = MagicMock()
    client.get.side_effect = fake_get

    resp = rss_fetcher._http_get_with_optional_5xx_retry(
        client,
        "https://hnrss.org/newest?q=SBOM",
        headers={},
        timeout_s=5.0,
        source_id="hn_sbom",
        sleep_fn=sleeps.append,
    )
    assert resp.status_code == 200
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_non_hn_source_does_not_retry_5xx() -> None:
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(502)

    client = MagicMock()
    client.get.side_effect = fake_get
    resp = rss_fetcher._http_get_with_optional_5xx_retry(
        client,
        "https://example.com/feed",
        headers={},
        timeout_s=5.0,
        source_id="snyk_blog",
        sleep_fn=lambda _d: None,
    )
    assert resp.status_code == 502
    assert calls["n"] == 1
