# CI Intel Tool

Stage 1 take-home for the **GenAI & Competitive Intelligence Engineer** role at JFrog: a working competitive-intelligence system that delivers a **daily digest** of industry and competitor news, scores relevance transparently, and shows a **sourced comparison matrix** of JFrog vs core competitors.

This is a real, runnable solution (not a mockup): RSS/Atom ingestion → dedupe → Gemini classification → weighted scoring in code → SQLite → Streamlit UI.

---

## Setup

```bash
cd ci-intel-tool
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set a real Gemini API key:

```
GEMINI_API_KEY=your-gemini-api-key-here
```

Get a key from [Google AI Studio](https://aistudio.google.com/apikey). Never commit `.env` (it is gitignored).

---

## Seed database + run the UI

For a seamless offline demo (no network / no API key required for browsing):

```bash
python scripts/seed_db.py
streamlit run src/ui/app.py
```

- `scripts/seed_db.py` rebuilds `data/seed.db` with several days of sample news, dimension scores, pipeline run history, and sample 👍/👎 feedback.
- The UI opens `data/ci_intel.db` when it has news rows; otherwise it falls back to `data/seed.db` (`resolve_db_path` in `src/db/connection.py`).
- Runtime DB `data/ci_intel.db` is gitignored; committed `data/seed.db` keeps demos working when the internet or a feed is down.

### UI tabs

| Tab | What you get |
|-----|----------------|
| **Daily Digest** | Items sorted by relevance; weight sliders + **Save weights**; 👍/👎 + rationale; **Run Now**; pipeline run history |
| **Ask the Digest** | Light RAG: retrieve news from DB → Gemini answers with citations only from those rows |
| **Comparison** | Curated capability matrix (`last_reviewed` shown) — every claim has a source link + quote, or **Unknown** |

### Optional shared database (Turso)

So every reviewer sees the **same** digest, weights, and feedback, set in `.env`:

```
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...
```

Then restart the app / pipeline. If those vars are unset (or still placeholders), the tool uses local SQLite (`data/ci_intel.db` / `data/seed.db`).

**Production note:** For a real multi-user product we would choose managed **PostgreSQL** (see [DECISIONS.md](DECISIONS.md)). Turso is the take-home choice to share one SQLite-compatible DB with minimal rewrite.

---

## Run the ingestion pipeline

Dry run (fetch + dedupe + print only — **no LLM calls, no DB writes**):

```bash
python -m src.pipeline.run_daily --dry-run
```

Live run (classifies new items with Gemini, writes to SQLite):

```bash
python -m src.pipeline.run_daily
python -m src.pipeline.run_daily --trigger cron
python -m src.pipeline.run_daily --limit 10
python -m src.pipeline.run_daily --db data/ci_intel.db
```

| Flag | Purpose |
|------|---------|
| `--dry-run` | Fetch + dedupe only |
| `--trigger manual\|cron` | Stored on `pipeline_runs` (default: `manual`) |
| `--limit N` | Cap new items to classify (also capped by `max_items_per_run` in `config/model.yaml`, currently **30**) |
| `--db PATH` | Override SQLite path |

Requires `GEMINI_API_KEY` for live classification.

---

## Verify feeds

```bash
python scripts/verify_feeds.py
python scripts/verify_feeds.py --sample
```

Checks enabled URLs in `config/sources.yaml`. Exit code is non-zero if any enabled source fails.

---

## Architecture overview

```
config/*.yaml          competitors, sources, weights, model, comparison
        │
        ▼
┌─────────────────┐    ┌──────────┐    ┌─────────────────────┐
│ RSS/Atom ingest │ →  │  Dedupe  │ →  │ Gemini (Pydantic    │
│ (httpx+feedparser)│  │ URL+hash │    │ structured dims 1–5)│
└─────────────────┘    └──────────┘    └──────────┬──────────┘
                                                   │
                                                   ▼
                                       ┌─────────────────────┐
                                       │ Weighted score in   │
                                       │ code (weights.yaml) │
                                       └──────────┬──────────┘
                                                   │
                                                   ▼
                                       ┌─────────────────────┐
                                       │ SQLite              │
                                       │ news + dims +       │
                                       │ feedback + runs     │
                                       └──────────┬──────────┘
                                                   │
                          ┌────────────────────────┼────────────────────────┐
                          ▼                        ▼                        ▼
                   Streamlit UI              Service layer            (Future) MCP
                   Digest / Compare          digest / comparison      search_news, …
                                             / feedback
```

**Built now:** ingestion, dedupe, LLM dimension scoring, code-side weighted total, feedback table + UI buttons, curated comparison matrix, seed DB, GitHub Actions cron.

**Service layer** (`src/services/`) is intentionally separate from Streamlit so a read-only MCP server can wrap the same functions later without rewriting business logic.

---

## Key decisions

| Decision | Why |
|----------|-----|
| **Gemini** (`gemini-3.8-flash` in `config/model.yaml`) | Free-tier friendly for a ~2-day take-home; model id lives only in config for one-line swaps |
| **Weights in code, not in the LLM** | Dimension scores (1–5) are stored; retuning weights needs no re-query; **Save weights** persists to DB |
| **Turso for shared demo; Postgres for production** | Shared reviewers now; managed Postgres if this were a real product |
| **Light RAG Ask tab** | Retrieve-from-SQLite then generate with citations; embeddings later at scale |
| **Curated `comparison.yaml`** | Claims must be source-linked; never generated from model memory; not auto-updated by news |
| **JFrog Medium RSS** | Official `jfrog.com/blog/feed/` returned empty (HTTP 202); Medium `@JFrog.com` feed verified working |
| **Community feeds (Reddit/HN)** | Market perception alongside official vendor blogs |
| **Feedback table + UI now; learning later** | Groundwork for a lead-scoring-style loop; automated weight adjustment is Future Work |

Full rationale: [DECISIONS.md](DECISIONS.md).

### Presentation point (feedback loop)

Today operators can nudge ranking with weight sliders and record 👍/👎 plus a short rationale. **In the future**, users will not tune weights manually. They will correct an item’s rating with a brief explanation (“Not relevant because…”), and an automated feedback loop will ingest that signal to adjust weights (and potentially prompts)—the same idea as a lead-scoring feedback loop. The storage and UI are built; the learning engine is not.

---

## Security Considerations

- Untrusted RSS text is isolated in LLM prompts behind `<<<UNTRUSTED_CONTENT>>>` … `<<<END_UNTRUSTED_CONTENT>>>` delimiters (prompt-injection awareness).
- No hardcoded secrets: API keys live in `.env` only; `.env.example` ships placeholders; `.gitignore` blocks `.env`.
- Comparison and news claims carry source URLs (or **Unknown**)—no claims from model memory.
- Cost caps: `max_items_per_run`, request timeout, excerpt length, and rate-limit sleep in `config/model.yaml`.
- In production, dependency scanning and package policy would use **JFrog Xray / Curation**; any future MCP tools would be vetted via a **JFrog MCP Registry** before deployment.

---

## Known limitations / Not Built

| Item | Status |
|------|--------|
| Automated weight learning from feedback | **Future Work** (table + UI + Save weights exist) |
| Embeddings / vector DB | **Future Work** — light RAG (keyword retrieve → Gemini) is built in Ask the Digest |
| Full multi-tenant Postgres production DB | Documented as production choice; take-home uses Turso or local SQLite |
| Slack notifications | Not built |
| Classification eval suite | Not built (unit tests cover scoring + dedupe) |
| Read-only MCP server (`search_news`, `get_comparison`, `get_digest`) | Optional bonus — **not built** |
| Secondary competitors (Cloudsmith, Harness) | Present in config, **`enabled: false`** |
| The Register feed | Disabled (bot-challenge HTML to automated clients) |

---

## UI note

The Streamlit app **emulates** a JFrog-like dark aesthetic (navy `#070B19`, green `#40BE46`, Open Sans via theme/CSS). It is **not** an official JFrog component library—no official logos or brand assets are copied.

**Streamlit is pinned** at `streamlit==1.39.0` in `requirements.txt` because custom CSS for pills/tabs is fragile across Streamlit releases. Keep that pin for demos.

---

## Daily cron (GitHub Actions)

Workflow: [`.github/workflows/daily_ingest.yml`](.github/workflows/daily_ingest.yml)

- Schedule: **06:00 UTC daily** + manual `workflow_dispatch`
- Runs: `python -m src.pipeline.run_daily --trigger cron`
- Uploads `data/ci_intel.db` as a workflow artifact (does **not** commit the DB back to git)

**Required:** repository secret `GEMINI_API_KEY` must be set under *Settings → Secrets and variables → Actions*, or the scheduled job will fail classification.

---

## Tests

```bash
pytest -q
```

Critical logic covered: weighted relevance scoring (`tests/test_scoring.py`), deduplication (`tests/test_dedupe.py`), and LLM classification helpers (`tests/test_llm_classify.py`).

---

## Project layout (high level)

```
config/           competitors, sources, weights, model, comparison
data/             schema.sql, seed.db (committed); ci_intel.db (gitignored)
scripts/          seed_db.py, verify_feeds.py
src/ingest/       RSS fetch + normalize
src/process/      dedupe, LLM classify, scoring
src/pipeline/     run_daily orchestrator
src/db/           SQLite connection, models, repository
src/services/     digest, comparison, feedback (UI-agnostic)
src/ui/           Streamlit app + styles.css
.github/workflows daily_ingest.yml
```

---

## Presentation materials

- [DECISIONS.md](DECISIONS.md) — architectural decision log
- [PRESENTATION_PREP.md](PRESENTATION_PREP.md) — 7–10 min script, demo outline, panel Q&As
