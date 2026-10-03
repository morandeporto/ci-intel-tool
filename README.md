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

## Run the UI

```bash
streamlit run src/ui/app.py
```

The app uses **Turso** when `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` are set in `.env`, otherwise local `data/ci_intel.db`. There is **no seed/fake database** - an empty DB shows a friendly empty state with **Run Now**.

### UI tabs

| Tab | What you get |
|-----|----------------|
| **Daily Digest** | Items by date (Israel today default), weight sliders, min-relevance filter, item_type filter, 👍/👎, **Run Now** |
| **Ask the Digest** | Light RAG: top-k news + curated comparison matrix → Gemini with citations, up to 2 session follow-ups |
| **Comparison** | Curated capability matrix - every claim has a source link + quote, or **Unknown** |
| **Pipeline runs** | Cron/manual run history plus per-source telemetry for the selected run |

### Optional shared database (Turso)

So every reviewer sees the **same** digest, weights, and feedback, set in `.env`:

```
TURSO_DATABASE_URL=libsql://...
TURSO_AUTH_TOKEN=...
```

Then restart the app / pipeline. If those vars are unset (or still placeholders), the tool uses local SQLite (`data/ci_intel.db`).

**Production note:** For a real multi-user product we would choose managed **PostgreSQL** (see [DECISIONS.md](DECISIONS.md)). Turso is the take-home choice to share one SQLite-compatible DB with minimal rewrite.

---

## Run the ingestion pipeline

Flow: concurrent fetch → URL dedupe → **48h freshness** → **relevance gate** →
**kind-balanced selection** (max **20** / run, **3** / source) → Gemini → SQLite/Turso.

Dry run (fetch + gate + select + print - **no LLM calls, no DB writes**):

```bash
python -m src.pipeline.run_daily --dry-run
```

Live run:

```bash
python -m src.pipeline.run_daily
python -m src.pipeline.run_daily --trigger cron
python -m src.pipeline.run_daily --limit 40          # can raise the cap for a run
python -m src.pipeline.run_daily --backfill-days 7 --limit 80
python -m src.pipeline.run_daily --db data/ci_intel.db
```

| Flag | Purpose |
|------|---------|
| `--dry-run` | Fetch + freshness + gate + select, no LLM, no writes |
| `--trigger manual\|cron` | Stored on `pipeline_runs` (default: `manual`) |
| `--limit N` | Selection/classify cap for this run (overrides `max_items_per_run: 20`) |
| `--backfill-days N` | Widen freshness window to N days (real ingest, same gate/selection) |
| `--db PATH` | Force local SQLite path (skips Turso) |

Requires `GEMINI_API_KEY` for live classification. Writes to **Turso** when configured, else `data/ci_intel.db`.

### Gate and selection (config-driven)

- **Freshness:** `window_hours: 48` in `config/model.yaml`. Future-dated items ignored, missing dates logged.
- **Gate:** `gate: "off"` for official/emerging, `gate: "strict"` for industry/community (`config/relevance.yaml` strong keywords, weak alone never pass). Maintenance title patterns excluded for all sources.
- **Selection:** top 3 per source, reserved slots official 8 / emerging 4 / industry+community 6, unused slots spill, cap-skipped items are **not** stored.

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
config/*.yaml   competitors, sources, relevance, weights, model, comparison
        │
        ▼
┌──────────────┐  ┌───────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐
│ Concurrent   │→ │ Dedupe│→ │ Freshness│→ │ Gate     │→ │ Select      │
│ RSS fetch    │  │ URL   │  │ 48h      │  │ off/strict│ │ kinds+caps  │
└──────────────┘  └───────┘  └──────────┘  └──────────┘  └──────┬──────┘
                                                                 ▼
                                                    Gemini → weighted score → Turso/SQLite
                                                                 │
                                    Streamlit (Digest / Ask / Comparison / Pipeline runs)
```

**Built now:** ingestion, freshness window, relevance gate, balanced selection, LLM dimension scoring, code-side weighted total, feedback table + UI buttons, curated comparison matrix, GitHub Actions cron (Turso).

**Service layer** (`src/services/`) is intentionally separate from Streamlit so a read-only MCP server can wrap the same functions later without rewriting business logic.

---

## Key decisions

| Decision | Why |
|----------|-----|
| **Gemini** (`gemini-3.8-flash` in `config/model.yaml`) | Free-tier friendly for a ~2-day take-home, model id lives only in config for one-line swaps |
| **Weights in code, not in the LLM** | Dimension scores (1-5) are stored, retuning weights needs no re-query, **Save weights** persists to DB |
| **Turso for shared demo, Postgres for production** | Shared reviewers now, managed Postgres if this were a real product |
| **Light RAG Ask tab** | Retrieve-from-SQLite + curated comparison matrix → Gemini, max 2 follow-ups per session thread |
| **Curated `comparison.yaml`** | Claims must be source-linked, never generated from model memory, not auto-updated by news |
| **48h window + gate + balanced selection** | Survives date-only stamps / missed cron, cheap keyword gate for noisy outlets, reserved LLM seats by kind |
| **Official JFrog blog + research RSS** | Verified feeds, Medium/status disabled once replacements passed |
| **Emerging + industry coverage** | Chainguard/Socket/Endor/Anchore/Docker + research/standards blogs (verified URLs only) |
| **Feedback table + UI now, learning later** | Groundwork for a lead-scoring-style loop, automated weight adjustment is Future Work |

Full rationale: [DECISIONS.md](DECISIONS.md).

### Presentation point (feedback loop)

Today operators can nudge ranking with weight sliders and record 👍/👎 plus a short rationale. **In the future**, users will not tune weights manually. They will correct an item’s rating with a brief explanation (“Not relevant because…”), and an automated feedback loop will ingest that signal to adjust weights (and potentially prompts)-the same idea as a lead-scoring feedback loop. The storage and UI are built, the learning engine is not.

---

## Security Considerations

- Untrusted RSS text is isolated in LLM prompts behind `<<<UNTRUSTED_CONTENT>>>` … `<<<END_UNTRUSTED_CONTENT>>>` delimiters (prompt-injection awareness).
- No hardcoded secrets: API keys live in `.env` only, `.env.example` ships placeholders, `.gitignore` blocks `.env`.
- Comparison and news claims carry source URLs (or **Unknown**)-no claims from model memory.
- Cost caps: `max_items_per_run`, request timeout, excerpt length, and rate-limit sleep in `config/model.yaml`.
- In production, dependency scanning and package policy would use **JFrog Xray / Curation**, any future MCP tools would be vetted via a **JFrog MCP Registry** before deployment.

---

## Known limitations / Not Built

| Item | Status |
|------|--------|
| Automated weight learning from feedback | **Future Work** (table + UI + Save weights exist) |
| Embeddings / vector DB | **Future Work** - light RAG (keyword retrieve → Gemini) is built in Ask the Digest |
| Full multi-tenant Postgres production DB | Documented as production choice, take-home uses Turso or local SQLite |
| Slack notifications | Not built |
| Classification eval suite | Not built (unit tests cover scoring + dedupe) |
| Read-only MCP server (`search_news`, `get_comparison`, `get_digest`) | Optional bonus - **not built** |
| Cloudsmith / Harness / emerging vendors | **Enabled** after feed verification (2026-10-03) |
| CISA advisories / CRA regulation feeds | **Not in** - HTTP 403 / not CRA-specific, **Future Work adapter** |
| The Register feed | Disabled (bot-challenge HTML to automated clients) |

### Challenges and pitfalls

| Pitfall | What we saw | Mitigation |
|---------|-------------|------------|
| Gemini free-tier **daily quota is per model** | `PerDay` errors stop generate, Ask/Run Now compete with cron | Soft `llm_usage` + `model_daily_limits` per model id, optional `fallback_model`, Ask shows a friendly message, remaining items become `pending_scoring` (not mid-score fallback) |
| Classification cost / rate | One call per item burned the free tier fast | **Batched classification** (`batch_size: 5`) with per-article delimiters, per-model min interval (RPM) |
| `pending_scoring` backlog | Quota mid-run left items unscored | Hide unscored in the UI by default, end-of-run heal + nightly `retry_pending.yml` (08:30 UTC) share the same soft budget and atomic `scoring` claims |
| HN / hnrss.org **502** | Intermittent gateway errors on `hn_*` feeds | 5xx retry + backoff for `hn_*`, left enabled with YAML notes when still failing |
| Reddit noise / 429 | `r/devops` was 25/25 filtered under the strict gate | Disabled `reddit_devops` (2026-10-03) with YAML note |
| JFrog blog empty / HTTP 202 | `jfrog.com/blog/feed/` often returns 202 with empty body | Marked `user_agent: browser`, keep research feed, Medium disabled as stale |
| Browser-like User-Agent | Some hosts reject the honest tool UA | Default UA is identifiable `ci-intel-tool/1.0`, **only** `jfrog_blog` uses browser UA today (listed in YAML). We do **not** bypass 403/challenges |
| Date-only timestamps | Midnight stamps look “old” vs a 24h morning run | **48h** freshness window |
| Future-dated status items | Status feeds sometimes post future maintenance windows | Drop `published_at > now` |
| Huge archives | `snyk.io/blog/feed/` has ~1670 historical items | Parse/normalize **only in-window** entries before selection |
| Feeds that do not exist | Guessed `/blog/feed` paths 404, IR/CISA 403 | Verify with `scripts/verify_feeds.py`, disable with dated YAML notes |
| Fallback / silent success | Model outages could look like a green run | `is_fallback` flag, cron fails if **every** selected item falls back, high fallback → `degraded` |

### Why not every source type from the brief - and how we'd ship them in production

**Built now (v1):** verified **RSS/Atom only** - official + emerging vendor blogs, security research,
standards/community, HN/Reddit. We do **not** invent mock URLs.

| Brief source type | v1 status | Why |
|-------------------|-----------|-----|
| Official blogs / release notes | **In** | Core + secondary competitors with verified feeds |
| Emerging players | **In** | Chainguard, Socket, Endor Labs, Anchore, Docker |
| Security research / DevOps news | **In** | ReversingLabs, Aikido, StepSecurity, CNCF, OpenSSF, TNS, PyPI, … |
| Community | **In** (gated) | HN keyword feeds + Reddit, strict keyword gate |
| Regulation (CISA, EU CRA / SBOM rules) | **Not in** | CISA XML **403**, EU digital-strategy RSS is generic policy noise - needs a dedicated **regulation adapter** |
| Quarterly financials (JFrog, GitLab) | **Not in** | IR RSS **403** |
| Pricing / jobs | **Not in** | HTML change-detection / ATS APIs - future adapters |

**Production adapter roadmap** (same normalize → classify → score path): IR/EDGAR, CRA/CISA
regulation monitors, pricing diff, jobs APIs - with circuit breakers and ToS checks. Comparison
matrix stays curated YAML until an analyst promotes a sourced claim.

---

## UI note

The Streamlit app **emulates** a JFrog-like dark aesthetic (navy `#070B19`, green `#40BE46`, Open Sans via theme/CSS). It is **not** an official JFrog component library-no official logos or brand assets are copied.

**Streamlit is pinned** at `streamlit==1.39.0` in `requirements.txt` because custom CSS for pills/tabs is fragile across Streamlit releases. Keep that pin for demos.

---

## Daily cron (GitHub Actions)

Workflow: [`.github/workflows/daily_ingest.yml`](.github/workflows/daily_ingest.yml)

- Schedule: **06:00 UTC daily** + manual `workflow_dispatch`
- Pings Turso, then runs `python -m src.pipeline.run_daily --trigger cron` writing to Turso
- Fails the job if Turso is unreachable or every model call falls back
- Optional local SQLite artifact upload is **debug-only** (`if-no-files-found: ignore`)

**Required repository secrets:** `GEMINI_API_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`.

---

## Tests

```bash
pytest -q
```

Critical logic covered: weighted scoring, dedupe, freshness window, relevance gate,
per-source/kind selection, config loading, schema migration, and LLM parse helpers.

---

## Project layout (high level)

```
config/           competitors, sources, weights, model, comparison
data/             schema.sql, ci_intel.db (gitignored, created at runtime)
scripts/          verify_feeds.py, diagnose_fetch.py
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

- [DECISIONS.md](DECISIONS.md) - architectural decision log
