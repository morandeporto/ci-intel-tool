-- CI Intel Tool SQLite schema
-- Dimension scores are stored separately so weights can be retuned without re-querying the LLM.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running | success | partial | failed
    trigger TEXT NOT NULL,             -- cron | manual | seed
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new INTEGER NOT NULL DEFAULT 0,
    items_scored INTEGER NOT NULL DEFAULT 0,
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
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_news_relevance ON news_items(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_news_content_hash ON news_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_news_competitor ON news_items(competitor);

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
