"""Unit tests for light RAG retrieval + Ask prompt grounding (no LLM calls)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.db.connection import get_connection, init_db
from src.db.models import NewsItem
from src.db.repository import Repository
from src.services.ask_digest import (
    MAX_FOLLOW_UPS,
    MAX_USER_TURNS,
    ChatTurn,
    MatrixClaimRef,
    RetrievedItem,
    ask_digest,
    build_ask_prompt,
    filter_context_urls,
    format_comparison_context,
    resolve_matrix_citations,
    retrieve_relevant_items,
)
from src.process.llm_classify import ClassifyError


def _seed_item(
    repo: Repository,
    *,
    item_id: str,
    title: str,
    url: str,
    competitor: str,
    summary: str,
    content_hash: str,
) -> None:
    repo.upsert_news_item(
        NewsItem(
            id=item_id,
            title=title,
            url=url,
            source_id=f"{competitor}_blog",
            competitor=competitor,
            published_at="2026-10-01",
            ingested_at="2026-10-01T00:00:00+00:00",
            summary=summary,
            category="product_release",
            raw_excerpt=summary,
            content_hash=content_hash,
            relevance_score=4.0,
            run_id=None,
        )
    )


def _mock_gemini(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Install a fake google.generativeai that returns canned answers (no network)."""
    queue = list(answers)
    captured: list[str] = []

    class _FakeModel:
        def __init__(self, *_a, **_k) -> None:
            pass

        def generate_content(self, prompt, **_kwargs):  # noqa: ANN001
            captured.append(str(prompt))
            text = queue.pop(0) if queue else "empty"
            return SimpleNamespace(text=text)

    fake_genai = SimpleNamespace(
        configure=lambda **_k: None,
        GenerativeModel=_FakeModel,
    )
    fake_types = SimpleNamespace(
        GenerationConfig=lambda **_k: None,
        RequestOptions=lambda **_k: None,
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setitem(__import__("sys").modules, "google.generativeai", fake_genai)
    monkeypatch.setitem(
        __import__("sys").modules, "google.generativeai.types", fake_types
    )
    monkeypatch.setattr(
        "src.services.ask_digest.wait_llm_interval", lambda: None
    )
    monkeypatch.setattr(
        "src.services.ask_digest.configure_llm_interval", lambda *_a, **_k: None
    )
    return captured


def test_retrieve_ranks_keyword_overlap(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)

    _seed_item(
        repo,
        item_id="1",
        title="Snyk launches new SCA feature",
        url="https://example.com/snyk",
        competitor="snyk",
        summary="Snyk improved open source scanning",
        content_hash="a" * 64,
    )
    _seed_item(
        repo,
        item_id="2",
        title="Unrelated cooking recipes",
        url="https://example.com/food",
        competitor="industry",
        summary="pasta",
        content_hash="b" * 64,
    )

    hits = retrieve_relevant_items(repo, "What did Snyk announce about SCA?", top_k=5)
    assert hits
    assert hits[0].id == "1"
    conn.close()


def test_format_comparison_includes_sourced_claims():
    text, refs = format_comparison_context()
    assert "Capability:" in text
    assert "source=" in text or "Unknown" in text
    assert "jfrog" in text.lower() or "JFrog" in text
    assert refs
    assert refs[0].mid == "M1"
    assert f"[{refs[0].mid}]" in text
    assert refs[0].source_url.startswith("http")


def test_build_ask_prompt_includes_matrix_and_news_and_history():
    items = [
        RetrievedItem(
            id="1",
            title="Snyk news",
            url="https://example.com/1",
            summary="SCA update",
            competitor="snyk",
            relevance_score=4.0,
            score=2.0,
        )
    ]
    prompt = build_ask_prompt(
        "How does that compare to Artifactory?",
        items,
        comparison_text="Capability: Artifact management\n  - JFrog: Artifactory | source=https://jfrog.com",
        history=[
            ChatTurn(role="user", content="What did Snyk announce?"),
            ChatTurn(role="assistant", content="Snyk announced SCA updates [1]."),
        ],
    )
    assert "PRODUCT_COMPARISON" in prompt
    assert "RETRIEVED_NEWS" in prompt
    assert "Snyk news" in prompt
    assert "Artifactory" in prompt
    assert "PRIOR CONVERSATION" in prompt
    assert "What did Snyk announce?" in prompt
    assert "neutral competitive-intelligence analyst" in prompt
    assert "**What happened**" in prompt
    assert "**What it means for JFrog**" in prompt
    assert "no marketing" in prompt.lower() or "no superlatives" in prompt
    assert "Do NOT state product capabilities that are not present" in prompt


def test_filter_context_urls_drops_unknown_and_javascript(caplog):
    allowed = {"https://jfrog.com/xray/", "https://example.com/news"}
    # Valid context URL kept.
    assert filter_context_urls(["https://jfrog.com/xray/"], allowed) == [
        "https://jfrog.com/xray/"
    ]
    # Unknown URL dropped.
    with caplog.at_level("WARNING"):
        assert filter_context_urls(["https://evil.example/phish"], allowed) == []
    assert any("not in Ask context" in r.message for r in caplog.records)
    caplog.clear()
    # javascript: dropped.
    with caplog.at_level("WARNING"):
        assert (
            filter_context_urls(["javascript:alert(1)"], allowed) == []
        )
    assert any("non-http" in r.message for r in caplog.records)


def test_resolve_matrix_citations_renders_valid_only():
    refs = [
        MatrixClaimRef(
            mid="M1",
            company_label="JFrog",
            capability_label="Security Scanning",
            claim="Xray + Curation",
            source_url="https://jfrog.com/xray/",
        ),
        MatrixClaimRef(
            mid="M2",
            company_label="Snyk",
            capability_label="Security Scanning",
            claim="Snyk Code",
            source_url="https://snyk.io/product/",
        ),
    ]
    allowed = {"https://jfrog.com/xray/", "https://snyk.io/product/"}
    answer = "JFrog offers Xray [M1]. Ignore invented [M99]."
    resolved = resolve_matrix_citations(answer, refs, allowed)
    assert [r.mid for r in resolved] == ["M1"]
    assert resolved[0].source_url == "https://jfrog.com/xray/"

    # Even a known mid is dropped if its URL is not in the allowlist.
    tight = {"https://example.com/news-only"}
    assert resolve_matrix_citations("See [M1]", refs, tight) == []


def test_ask_digest_rejects_oversized_thread(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)
    history = []
    for i in range(MAX_USER_TURNS):
        history.append(ChatTurn(role="user", content=f"q{i}"))
        history.append(ChatTurn(role="assistant", content=f"a{i}"))
    assert MAX_FOLLOW_UPS == 2
    with pytest.raises(ClassifyError, match="follow-up"):
        ask_digest(repo, "one more?", history=history)
    conn.close()


def test_per_turn_sources_two_turns(tmp_path, monkeypatch):
    """Each assistant turn keeps its own retrieved Sources; [1] is turn-local."""
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)

    snyk_item = RetrievedItem(
        id="snyk1",
        title="Snyk launches SCA feature",
        url="https://example.com/snyk-sca",
        summary="Snyk improved SCA scanning",
        competitor="snyk",
        relevance_score=4.0,
        score=3.0,
    )
    github_item = RetrievedItem(
        id="gh1",
        title="GitHub announces Copilot Workspace",
        url="https://example.com/github-ai",
        summary="GitHub Copilot Workspace for AI coding",
        competitor="github",
        relevance_score=4.0,
        score=3.0,
    )
    # Deterministic per-turn retrieval (avoid prior-question token bleed).
    retrieve_queue = [[snyk_item], [github_item]]

    def _fake_retrieve(*_a, **_k):
        return retrieve_queue.pop(0)

    monkeypatch.setattr(
        "src.services.ask_digest.retrieve_relevant_items", _fake_retrieve
    )
    _mock_gemini(
        monkeypatch,
        [
            "Snyk announced an SCA update [1].",
            "GitHub announced Copilot Workspace [1].",
        ],
    )
    model_cfg = {
        "ask_model": "gemini-3.1-flash-lite",
        "model_daily_limits": {"gemini-3.1-flash-lite": 50},
        "quota_day_timezone": "UTC",
        "request_timeout_seconds": 60,
    }

    turn1 = ask_digest(
        repo,
        "What did Snyk announce about SCA?",
        model_config=model_cfg,
    )
    assert [c.url for c in turn1.citations] == ["https://example.com/snyk-sca"]
    assert "[1]" in turn1.answer

    history = [
        ChatTurn(role="user", content="What did Snyk announce about SCA?"),
        ChatTurn(role="assistant", content=turn1.answer),
    ]
    turn2 = ask_digest(
        repo,
        "What did GitHub announce about AI?",
        history=history,
        model_config=model_cfg,
    )
    assert [c.url for c in turn2.citations] == ["https://example.com/github-ai"]
    # Turn-local numbering: each turn's [1] maps to that turn's Sources only.
    assert turn1.citations[0].url != turn2.citations[0].url

    # Simulate UI thread storage: each assistant turn carries its own citations.
    thread = [
        {"role": "user", "content": "What did Snyk announce about SCA?"},
        {
            "role": "assistant",
            "content": turn1.answer,
            "citations": [
                {"title": c.title, "url": c.url, "competitor": c.competitor}
                for c in turn1.citations
            ],
        },
        {"role": "user", "content": "What did GitHub announce about AI?"},
        {
            "role": "assistant",
            "content": turn2.answer,
            "citations": [
                {"title": c.title, "url": c.url, "competitor": c.competitor}
                for c in turn2.citations
            ],
        },
    ]
    assistant_turns = [t for t in thread if t["role"] == "assistant"]
    assert assistant_turns[0]["citations"][0]["url"] == "https://example.com/snyk-sca"
    assert assistant_turns[1]["citations"][0]["url"] == "https://example.com/github-ai"

    conn.close()
