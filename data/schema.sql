-- CI Intel Tool SQLite schema
-- Dimension scores are stored separately so weights can be retuned without re-querying the LLM.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running | success | partial | degraded | failed
    trigger TEXT NOT NULL,             -- cron | manual | seed
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_scored INTEGER NOT NULL DEFAULT 0,
    items_classified_ok INTEGER NOT NULL DEFAULT 0,
    items_fallback INTEGER NOT NULL DEFAULT 0,
    retries_used INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    competitor TEXT NOT NULL,
    published_at TEXT,
    ingested_at TEXT NOT NULL,
    summary TEXT,
    category TEXT,
    raw_excerpt TEXT,
    content_hash TEXT NOT NULL,
    relevance_score REAL,
    run_id TEXT,
    -- classified | filtered | pending_scoring | scoring
    status TEXT NOT NULL DEFAULT 'classified',
    filter_reason TEXT,
    -- LLM fields: competitor | emerging | industry
    item_type TEXT,
    jfrog_implication TEXT,
    -- 1 when Gemini failed and mid-score placeholders were stored
    is_fallback INTEGER NOT NULL DEFAULT 0,
    -- Exact Gemini model id that produced the classification (null if unscored)
    scored_by_model TEXT,
    -- Scoring rubric version from config/model.yaml (null for pre-versioning rows)
    rubric_version TEXT,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_news_relevance ON news_items(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_news_content_hash ON news_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_news_competitor ON news_items(competitor);
CREATE INDEX IF NOT EXISTS idx_news_status ON news_items(status);

CREATE TABLE IF NOT EXISTS dimension_scores (
    news_item_id TEXT PRIMARY KEY,
    jfrog_relevance INTEGER NOT NULL CHECK (jfrog_relevance BETWEEN 1 AND 5),
    competitor_signal INTEGER NOT NULL CHECK (competitor_signal BETWEEN 1 AND 5),
    strategic_impact INTEGER NOT NULL CHECK (strategic_impact BETWEEN 1 AND 5),
    freshness INTEGER NOT NULL CHECK (freshness BETWEEN 1 AND 5),
    market_visibility INTEGER NOT NULL CHECK (market_visibility BETWEEN 1 AND 5),
    model_id TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    FOREIGN KEY (news_item_id) REFERENCES news_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id TEXT NOT NULL,
    original_score REAL NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('up', 'down')),
    rationale TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (news_item_id) REFERENCES news_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedback_item ON feedback(news_item_id);

-- Persistent app settings (e.g. saved weight overrides shared across reviewers).
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Per-source telemetry for one pipeline run (diagnose fetch/gate/selection failures).
CREATE TABLE IF NOT EXISTS source_run_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    http_status TEXT,
    fetched INTEGER NOT NULL DEFAULT 0,
    in_window INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    passed_gate INTEGER NOT NULL DEFAULT 0,
    selected INTEGER NOT NULL DEFAULT 0,
    classified INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    warning TEXT,
    duration_ms INTEGER,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_source_run_stats_run ON source_run_stats(run_id);

-- Soft per-model daily call counters (pipeline | ask | rescore). Hard stop is API PerDay.
CREATE TABLE IF NOT EXISTS llm_usage (
    date_utc TEXT NOT NULL,
    purpose TEXT NOT NULL,             -- pipeline | ask | rescore
    model TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date_utc, purpose, model)
);
