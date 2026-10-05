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
    CitationRegistry,
    MatrixClaimRef,
    RetrievedItem,
    ask_digest,
    build_ask_prompt,
    build_conversation_sources,
    extract_cited_news_nums,
    filter_context_urls,
    format_comparison_context,
    is_safe_http_url,
    neutralize_unsafe_links,
    resolve_matrix_citations,
    retrieve_relevant_items,
    sanitize_answer_citations,
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


def _model_cfg() -> dict:
    return {
        "ask_model": "gemini-3.1-flash-lite",
        "model_daily_limits": {"gemini-3.1-flash-lite": 50},
        "quota_day_timezone": "UTC",
        "request_timeout_seconds": 60,
    }


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
        (
            3,
            RetrievedItem(
                id="1",
                title="Snyk news",
                url="https://example.com/1",
                summary="SCA update",
                competitor="snyk",
                relevance_score=4.0,
                score=2.0,
            ),
        )
    ]
    prompt = build_ask_prompt(
        "How does that compare to Artifactory?",
        items,
        comparison_text="Capability: Artifact management\n  - JFrog: Artifactory | source=https://jfrog.com",
        history=[
            ChatTurn(role="user", content="What did Snyk announce?"),
            ChatTurn(role="assistant", content="Snyk announced SCA updates [3]."),
        ],
    )
    assert "PRODUCT_COMPARISON" in prompt
    assert "RETRIEVED_NEWS" in prompt
    assert "[3]" in prompt
    assert "Snyk news" in prompt
    assert "Artifactory" in prompt
    assert "PRIOR CONVERSATION" in prompt
    assert "What did Snyk announce?" in prompt
    assert "neutral competitive-intelligence analyst" in prompt
    assert "**What happened**" in prompt
    assert "**What it means for JFrog**" in prompt
    assert "no marketing" in prompt.lower() or "no superlatives" in prompt
    assert "Do NOT state product capabilities that are not present" in prompt
    assert "stable across the conversation" in prompt
    assert "the comparison matrix has no entry for" in prompt
    assert 'Never state that JFrog or a competitor "lacks"' in prompt


def test_filter_context_urls_drops_unknown_and_javascript(caplog):
    allowed = {"https://jfrog.com/xray/", "https://example.com/news"}
    assert filter_context_urls(["https://jfrog.com/xray/"], allowed) == [
        "https://jfrog.com/xray/"
    ]
    with caplog.at_level("WARNING"):
        assert filter_context_urls(["https://evil.example/phish"], allowed) == []
    assert any("not in Ask context" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert filter_context_urls(["javascript:alert(1)"], allowed) == []
    assert any("non-http" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jfrog.com/xray/", True),
        ("http://example.com/a", True),
        ("javascript:alert(1)", False),
        ("JavaScript:alert(1)", False),
        ("data:text/html;base64,PHNjcmlwdD4=", False),
        ("vbscript:msgbox(1)", False),
        ("//evil.example/x", False),
        ("https://", False),
        ("", False),
    ],
)
def test_is_safe_http_url_scheme_check(url, expected):
    assert is_safe_http_url(url) is expected


def test_neutralize_unsafe_links_keeps_http_and_strips_other_schemes():
    text = (
        "See [docs](https://jfrog.com/xray/) and [click](javascript:alert(1)) "
        "or <javascript:alert(2)> and [x](data:text/html,hi)."
    )
    cleaned = neutralize_unsafe_links(text)
    assert "[docs](https://jfrog.com/xray/)" in cleaned
    assert "](javascript:" not in cleaned
    assert "<javascript:" not in cleaned
    assert "](data:" not in cleaned
    assert "click" in cleaned


def test_news_card_drops_non_http_source_href():
    from src.ui.components import render_news_card

    card = render_news_card({"title": "t", "url": "javascript:alert(1)"})
    assert "href=" not in card
    safe = render_news_card({"title": "t", "url": "https://example.com/a"})
    assert 'href="https://example.com/a"' in safe


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

    tight = {"https://example.com/news-only"}
    assert resolve_matrix_citations("See [M1]", refs, tight) == []


def test_sanitize_removes_out_of_context_ids(caplog):
    with caplog.at_level("WARNING"):
        cleaned = sanitize_answer_citations(
            "Fact [1] and bad [9] plus [M2] and fake [M99].",
            allowed_news_nums={1},
            allowed_matrix_ids={"M2"},
        )
    assert "[1]" in cleaned
    assert "[M2]" in cleaned
    assert "[9]" not in cleaned
    assert "[M99]" not in cleaned
    assert any("Removed news citation [9]" in r.message for r in caplog.records)
    assert any("Removed matrix citation [M99]" in r.message for r in caplog.records)


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


def test_conversation_sources_cited_only_stable_and_shared_url():
    """Bottom Sources: stable ids, uncited absent, both Mids with same URL listed."""
    registry = CitationRegistry()
    shared = RetrievedItem(
        id="shared1",
        title="Shared news",
        url="https://example.com/shared",
        summary="s",
        competitor="github",
        relevance_score=4.0,
        score=5.0,
    )
    uncited = RetrievedItem(
        id="extra1",
        title="Uncited",
        url="https://example.com/extra",
        summary="e",
        competitor="snyk",
        relevance_score=3.0,
        score=2.0,
    )
    newer = RetrievedItem(
        id="new2",
        title="New item",
        url="https://example.com/new2",
        summary="n",
        competitor="harness",
        relevance_score=4.0,
        score=4.0,
    )
    registry.assign([shared, uncited])
    registry.assign([shared, newer])
    assert registry.number_for("shared1") == 1
    assert registry.number_for("extra1") == 2
    assert registry.number_for("new2") == 3

    matrix_refs = [
        MatrixClaimRef(
            mid="M1",
            company_label="JFrog",
            capability_label="Security",
            claim="Xray",
            source_url="https://shared.example/docs",
        ),
        MatrixClaimRef(
            mid="M2",
            company_label="Sonatype",
            capability_label="Security",
            claim="Firewall",
            source_url="https://shared.example/docs",
        ),
    ]
    answers = [
        "Shared news [1] and matrix [M1].",
        "Shared again [1], new [3], compare [M1] [M2].",
    ]
    sources = build_conversation_sources(answers, registry, matrix_refs)
    keys = [s.key for s in sources]
    # Every number in the text appears; uncited [2] absent.
    assert "1" in keys
    assert "3" in keys
    assert "2" not in keys
    assert extract_cited_news_nums("\n".join(answers)) == [1, 3]
    # Both matrix ids listed even though they share one URL.
    assert keys.count("M1") == 1
    assert keys.count("M2") == 1
    assert "M1" in keys and "M2" in keys
    urls = {s.key: s.url for s in sources}
    assert urls["M1"] == urls["M2"] == "https://shared.example/docs"
    # Sorted: news by number, then matrix by M number.
    assert keys == ["1", "3", "M1", "M2"]


def test_stable_number_passed_to_model_across_turns(tmp_path, monkeypatch):
    """Item retrieved in two turns keeps one number in prompts and answers."""
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    repo = Repository(conn)

    shared = RetrievedItem(
        id="shared1",
        title="Shared news item",
        url="https://example.com/shared",
        summary="Appears in both turns",
        competitor="github",
        relevance_score=4.0,
        score=5.0,
    )
    newer = RetrievedItem(
        id="new2",
        title="Second-turn news",
        url="https://example.com/new2",
        summary="New in turn two",
        competitor="harness",
        relevance_score=4.0,
        score=4.0,
    )
    retrieve_queue = [[shared], [shared, newer]]

    def _fake_retrieve(*_a, **_k):
        return retrieve_queue.pop(0)

    monkeypatch.setattr(
        "src.services.ask_digest.retrieve_relevant_items", _fake_retrieve
    )
    monkeypatch.setattr(
        "src.services.ask_digest.format_comparison_context",
        lambda _c=None: ("Capability: none", []),
    )
    prompts = _mock_gemini(
        monkeypatch,
        [
            "**What happened** Shared [1].\n**What it means for JFrog** Note.",
            "**What happened** Shared [1] and new [2].\n**What it means for JFrog** Note.",
        ],
    )
    registry = CitationRegistry()
    turn1 = ask_digest(
        repo, "q1", model_config=_model_cfg(), citation_registry=registry
    )
    turn2 = ask_digest(
        repo,
        "q2",
        history=[
            ChatTurn(role="user", content="q1"),
            ChatTurn(role="assistant", content=turn1.answer),
        ],
        model_config=_model_cfg(),
        citation_registry=registry,
    )
    assert registry.number_for("shared1") == 1
    assert registry.number_for("new2") == 2
    assert "[1]" in prompts[0] and "shared1" in prompts[0]
    assert "[1]" in prompts[1] and "shared1" in prompts[1]
    assert "[2]" in prompts[1] and "new2" in prompts[1]
    assert "[1]" in turn1.answer and "[1]" in turn2.answer
    assert "[2]" in turn2.answer

    answers = [turn1.answer, turn2.answer]
    sources = build_conversation_sources(answers, registry, [])
    assert [s.key for s in sources] == ["1", "2"]
    # Every cite in text is in the bottom list.
    for num in extract_cited_news_nums("\n".join(answers)):
        assert str(num) in {s.key for s in sources}
    conn.close()


def test_new_chat_resets_numbering():
    registry = CitationRegistry()
    item = RetrievedItem(
        id="a",
        title="A",
        url="https://example.com/a",
        summary="a",
        competitor="x",
        relevance_score=1.0,
        score=1.0,
    )
    registry.assign([item])
    assert registry.number_for("a") == 1
    fresh = CitationRegistry.from_dict(None)
    fresh.assign([item])
    assert fresh.number_for("a") == 1
    assert fresh._next_num == 2
