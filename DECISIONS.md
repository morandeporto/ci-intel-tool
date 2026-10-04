# Decisions Log

A running record of architectural decisions made during the project, newest first.
Maintained as decisions are made (see `.cursorrules`).

---

## [2026-10-04] Persist hard PerDay `blocked_until` per model

**Selected Option:** On a hard Gemini PerDay error, parse the retry hint
(e.g. `18h55m33s`), store `blocked_until = now + hint + 120s` in
`llm_model_blocks`, and skip API calls for that model until then. Pipeline
marks items `pending_scoring` (or switches to `fallback_model` if unblocked);
Ask shows a friendly message with UTC + Asia/Jerusalem reset times.

**Alternatives Considered:**
- Rely only on soft `llm_usage` budgets (rejected: scripts/UI can still burn
  the hard free-tier cap)
- Process-local in-memory cooldown (rejected: lost across cron / Ask / scripts)
- Hardcode a reset hour (rejected: provider hint is the source of truth)

**Rationale:** Cost/Reliability - one shared cooldown across all callers
(SQLite or Turso) stops wasted generate attempts after the hard daily quota.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "When Google returns PerDay, we parse the retry hint, persist blocked_until
> with a small safety margin, and every path — pipeline, Ask, compare scripts —
> checks that row before calling the API. Soft budgets stay advisory; the hard
> block is the real backstop."

**JFrog Product Connection (If applicable):**
Similar to Xray/Artifactory rate-limit cooldowns — respect the upstream quota
signal once, then fail closed until the window reopens.

---

## [2026-10-03] UI Run Now limit as config (`ui_run_now_limit`)

**Selected Option:** Cap Streamlit **Run Now** via `ui_run_now_limit` in
`config/model.yaml` (default **10**). Cron / CLI still use `max_items_per_run` (20)
unless `--limit` overrides.

**Alternatives Considered:**
- Hardcode `limit=10` in `app.py` (rejected: invisible to operators)
- Same cap as cron (rejected: one demo click can exhaust free-tier RPD)
- No UI ingest button (rejected: live demo requirement)

**Rationale:** Cost/Latency - protect Gemini free-tier headroom during panel demos
while keeping cron’s fuller daily selection.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Run Now is intentionally cheaper than the nightly job. The limit lives in YAML
> next to max_items_per_run so we can raise it when quota allows without touching UI
> code."

**JFrog Product Connection (If applicable):**
Similar to rate limits / quotas on Artifactory APIs - interactive paths get tighter
budgets than scheduled batch jobs.

---

## [2026-10-03] Radio navigation instead of st.tabs

**Selected Option:** Horizontal `st.radio` keyed `_ci_main_tab` for Daily Digest /
Ask / Comparison / Pipeline runs, instead of `st.tabs`.

**Alternatives Considered:**
- `st.tabs` (rejected: Streamlit resets to the first tab on every rerun, which
  breaks Ask follow-ups and the two-phase Run Now / toast UX)
- Query-param deep links (rejected: extra complexity for a take-home)

**Rationale:** UX reliability - pending actions and Ask answers must leave the user
on the tab they were using after `st.rerun()`.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Streamlit tabs look right but reset on rerun. A keyed radio keeps the active
> section in session_state, which matters when Run Now or Ask triggers a two-step
> loader and refresh."

**JFrog Product Connection (If applicable):**
Not product-specific - same lesson as preferring stable control state over fragile
UI widgets when the platform remounts on each action.

---

## [2026-10-03] Digest date filter uses ingestion day (Asia/Jerusalem)

**Selected Option:** Daily Digest “News date” filters by the Israel calendar day of
`ingested_at` (fallback to `published_at` only if ingest is missing). Cards still
show the article’s **published** time. Default selection is Israel today, else the
latest day that has items.

**Alternatives Considered:**
- Filter by `published_at` only (rejected: late UTC ingest / date-only midnight
  stamps scatter “today’s digest” across calendar days)
- UTC day boundaries in the UI (rejected: reviewers are in Israel TZ for this demo)
- Multi-day range picker (rejected: adds clutter; single-day matches “daily digest”)

**Rationale:** Clarity - “what did the system bring in today” is the digest’s job;
publish time remains on the card for source fidelity.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "The filter answers when we ingested the item in Israel local time. The card still
> shows when the article was published. That split keeps a daily briefing coherent
> even when feeds use date-only timestamps."

**JFrog Product Connection (If applicable):**
Similar to separating scan time from component publish time in Xray - operators care
about when the signal entered the system as well as when the artifact existed.

---

## [2026-10-03] Scoring rubric v2 + rubric_version column

**Selected Option:** Tighten LLM scoring anchors in `SCORING_CALIBRATION_CURRENT`
(separate `strategic_impact` from `jfrog_relevance`, define supply-chain attack
scale 3/4/5, JFrog blog not auto-5, product-name mentions only when the product
is the subject). Keep numeric weights unchanged; store `rubric_version`
(`config/model.yaml`, e.g. `2026-10-v2`) on each newly classified item via
idempotent SQLite/Turso migration. Do not auto-rescore existing rows. Keep
`SCORING_CALIBRATION_LEGACY` for A/B via `legacy_rubric=` / `compare_models.py`.

**Alternatives Considered:**
- Re-score all history on rubric bump (rejected: burns quota; history stays
  comparable only within its stored `rubric_version`)
- Encode anchors only in `weights.yaml` descriptions (rejected: the model never
  sees those; prompts need the anchors)
- Drop legacy prompt text (rejected: need a controlled A/B before trusting v2)

**Rationale:** Calibration + auditability - clearer anchors cut false highs;
versioning lets us explain why two days' scores differ without rewriting the past.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "We tightened the scoring rubric so strategic impact is about breadth and
> lasting effect, not a copy of JFrog relevance, and we stamp each item with a
> rubric_version so we can change anchors later without silently rewriting
> history - A/B is a throwaway script against stored titles, not a DB rewrite."

**JFrog Product Connection (If applicable):**
Similar to Xray policy versions: scan results stay tied to the rule set that
produced them so auditors can explain score changes over time.

---

## [2026-10-03] Self-healing scoring: pending state, automatic rescore, nightly retry

**Selected Option:** On daily PerDay quota exhaustion, persist remaining items as
`status=pending_scoring` (not mid-score `is_fallback`). End-of-run rescore and a
nightly GHA job (`.github/workflows/retry_pending.yml`, 08:30 UTC) reclaim those
rows (plus recent fallbacks) via atomic `status='scoring'` claims, batched Gemini
calls, and a shared soft `llm_usage` budget. Stuck `scoring` rows >30 minutes
return to `pending_scoring`. Daily + retry workflows share a concurrency group.

**Alternatives Considered:**
- Mid-score fallback for quota (rejected: pollutes ranking / looks “scored”)
- Manual-only `--rescore-fallbacks` (rejected: demo/ops still break overnight)
- Separate Ask API key (rejected: single `GEMINI_API_KEY` keeps the take-home simple)

**Rationale:** Reliability + cost - free-tier PerDay caps are real, pending state
lets the system heal after reset without lying about scores.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "When Gemini hits its daily quota we stop immediately, park unscored items as
> pending_scoring, and retry them at the end of the run and again at night after
> the free-tier reset - with atomic claims so two jobs never classify the same row."

**JFrog Product Connection (If applicable):**
Similar to deferred deep scans that re-queue when a scanner is capacity-limited
rather than writing a fake “clean” result.

---

## [2026-10-03] Batched classification and per-model quota budgeting

**Selected Option:** Classify up to `batch_size: 5` items per Gemini call (JSON
array keyed by item id, unknown/duplicate ids rejected, missing → one individual
retry → fallback). Soft counters in `llm_usage` (`date`, `purpose`, `model`,
`calls`) with `model_daily_limits` and per-model min intervals. Hard stop on API
`PerDay` (or exhausted 429 retries). Config splits `pipeline_model`,
`fallback_model`, and `ask_model` (ids never guessed - use `scripts/list_gemini_models.py`).

**Alternatives Considered:**
- One call per item (rejected: burns the free tier)
- Hard-coded reset hour (rejected: use provider retry hint + cron safety margin)
- Shared global daily call cap only (rejected: Ask and pipeline need per-model limits)

**Rationale:** Cost + resilience - batching cuts RPM/RPD usage, per-model budgets
match how Google meters free tier.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "We batch five articles per classify call with injection-safe delimiters, track
> soft usage per model, and when PerDay hits we park items as pending instead of
> inventing average scores - optionally continuing on a configured fallback model."

**JFrog Product Connection (If applicable):**
Quota-aware workers echo Artifactory/Xray rate limiting - protect the shared
service so interactive users still get headroom.

---

## [2026-10-03] Relevance keyword expansion (strict gate)

**Selected Option:** Expand `strong_keywords` in `relevance.yaml` with CRA / Cyber
Resilience Act, Node.js, OSS security, dependency/package-manager terms,
supply-chain (hyphen), MCP, CloudBees, token theft / leaked credentials - without
changing weights or other thresholds. Disable `reddit_devops` after 25/25 filtered.

**Alternatives Considered:**
- Lower the gate to weak keywords (rejected: more noise into Gemini)
- Keep Reddit enabled for “coverage” (rejected: zero signal at 25/25 filtered)

**Rationale:** Signal quality - strict gate stays strict, vocabulary tracks real
CI/supply-chain language so industry/community items can pass when on-topic.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "The keyword gate is a cheap pre-filter before Gemini. I widened the strong list
> for CRA, OSS, and MCP-style terms, and turned off Reddit devops after it was
> pure noise - without touching score weights."

**JFrog Product Connection (If applicable):**
Policy allow-lists before expensive scans - same pattern as Curation policies.

---

## [2026-10-03] Pipeline degraded status for high fallback rate

**Selected Option:** Persist `items_classified_ok`, `items_fallback`, and
`retries_used` on `pipeline_runs`. Mark a run `status=degraded` when more than
30% of attempted classifications used average fallback scores (visible badge in
the Pipeline runs tab). Exit code 1 for both `failed` and `degraded`.

**Alternatives Considered:**
- Fold high fallback into `partial` only (rejected: operators cannot see outage vs mild noise)
- Fail the run at any fallback (rejected: too brittle for transient single-item errors)

**Rationale:** Reliability signal - Gemini 503 bursts must surface as degraded, not
quiet partial success, so cron/GHA and the UI can act.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "When the model is overloaded we retry with backoff, limit concurrency, and heal
> fallbacks on the next run. If more than 30% still fall back, the run is marked
> degraded with OK/fallback/retry counts in the Pipeline runs tab."

**JFrog Product Connection (If applicable):**
Same idea as Xray contextual severity - not every failure is equal, degraded tells
you the pipeline ran but quality is impaired.

---

## [2026-10-03] Source coverage: competitors, emerging players, industry

**Selected Option:** Tag every source with `kind` (`official_competitor` | `emerging` |
`industry` | `community`) and ingest only RSS/Atom URLs that pass HTTP verification.
Enable Chainguard/Socket/Endor/Anchore/Docker as emerging, industry research +
standards blogs, HN/Reddit as lower-trust community with `gate: strict`. Keep
regulation (CISA/CRA) and IR as Future Work adapters - they 403 or are not feeds.

**Alternatives Considered:**
- Fake/mock feed URLs for coverage (rejected: assignment forbids inventing URLs)
- Scrape HTML for CRA/CISA now (rejected: brittle, needs dedicated adapters)
- Vendor blogs only (rejected: digest was almost all competitor/JFrog content)

**Rationale:** Simplicity + honesty - broaden coverage inside the working RSS path;
document adapters for blocked regulation sources without pretending they are feeds.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "The digest used to be almost all competitor blogs. I added a source kind model -
> official, emerging, industry, community - and only turned on feeds I verified live.
> CISA and IR still 403 us, so those are future adapters into the same pipeline, not
> invented RSS rows."

**JFrog Product Connection (If applicable):**
Treating community HN feeds as lower-trust vs official vendor feeds mirrors
provenance tiers in Xray/Curation - noisy signals stay labeled and gated.

---

## [2026-10-03] Removing the seed database

**Selected Option:** Delete `data/seed.db` / `scripts/seed_db.py` and remove the
`resolve_db_path` seed fallback. The app always uses Turso when configured, else
local `data/ci_intel.db`. Empty DB shows a friendly empty state with **Run Now**.

**Alternatives Considered:**
- Keep seed for offline demos (rejected: fake news undermines trust in a CI tool)
- Commit a snapshot of real classified rows (rejected: stale + still not live)

**Rationale:** Honesty for reviewers - better an empty real DB than fabricated scores.
Shared Turso covers multi-reviewer demos without fake data.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "I removed the seed DB on purpose. Competitive intel should never show fake news.
> Reviewers share Turso when configured, locally you get an empty state and Run Now."

**JFrog Product Connection (If applicable):**
Same spirit as not promoting unscanned packages - do not present untrusted demo
artifacts as production signal.

---

## [2026-10-03] Selection: per-source cap and balanced slots across kinds

**Selected Option:** After the gate, keep top `max_per_source` (3) per source, then
fill reserved kind slots (`official_competitor` 8, `emerging` 4,
`industry_community` 6) with round-robin across sources up to `max_items_per_run`
(20). Unused reserved slots spill to other kinds. Cap-skipped items are not stored
so they can compete again inside the 48h window.

**Alternatives Considered:**
- YAML order + hard `[:20]` (rejected: competitor blogs dominate)
- Global recency sort only (rejected: one busy vendor consumes the budget)
- Store cap-skipped as filtered (rejected: blocks re-competition next run)

**Rationale:** Cost + coverage - Gemini spend stays bounded while emerging/industry
still get reserved seats.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Selection is like a fair queue: three items max per source, reserved seats for
> official vs emerging vs industry, round-robin inside each bucket, unused seats
> spill over. Cap-skipped rows are not written so they can win tomorrow."

**JFrog Product Connection (If applicable):**
Analogous to rate-limiting and fair scheduling of scan/enrichment workers so one
noisy repository cannot starve the rest.

---

## [2026-10-03] Per-source relevance gate (off for official, strict for industry/community)

**Selected Option:** `gate: "off"` for official competitor and emerging vendor feeds
(LLM judges relevance). `gate: "strict"` for industry/community: require ≥1 strong
whole-word keyword from `relevance.yaml`, weak keywords alone never pass. Shared
`exclude_title_patterns` drop maintenance titles on all sources. Filtered rows are
stored with `status=filtered` and hidden from the digest UI.

**Alternatives Considered:**
- Keyword-filter everything (rejected: kills low-volume vendor changelogs)
- LLM-classify everything then filter (rejected: burns budget on Reddit noise)
- Drop industry/community entirely (rejected: misses market/environment signal)

**Rationale:** Cost + signal quality - spend Gemini on items that already look
on-topic for noisy outlets, trust official/emerging volume to be self-selecting.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Official and emerging feeds skip the keyword gate - volume is low and the model
> decides. Industry and community must hit a strong keyword like SBOM or CVE as a
> whole word. Weak words like Docker never pass alone. Filtered items stay in the DB
> for audit but never hit Gemini or the digest."

**JFrog Product Connection (If applicable):**
Similar to policy gates before expensive deep scans - cheap deterministic filters
first, then heavier analysis.

---

## [2026-10-03] 48h window instead of 24h

**Selected Option:** Freshness window of **48 hours** (`window_hours` in
`config/model.yaml`). Ignore future-dated items. Log missing dates, do not crash.
Dedupe remains by URL. Future improvement: compute the freshness dimension in code
from `published_at` instead of asking the LLM (noted, not implemented yet).

**Alternatives Considered:**
- 24h window (rejected: date-only midnight stamps look >24h old on a morning run;
  one missed cron day loses a full day of news)
- No window / full archive (rejected: snyk_blog alone has ~1670 historical items)
- Soft window with LLM freshness only (rejected: still pays to normalize/score junk)

**Rationale:** Reliability + cost - URL dedupe makes a wider window free of
duplicates, 48h absorbs date-only timestamps and one failed daily run.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "We use 48 hours, not 24. Many feeds publish date-only at midnight, so a morning
> run would drop yesterday's posts under a 24h rule. If cron fails once, a 24h window
> loses a whole day. URL dedupe means the extra day does not create duplicates."

**JFrog Product Connection (If applicable):**
Operational resilience similar to retry/backfill windows in artifact indexing -
prefer a slightly wider catch window over silent data loss.

---

## [2026-10-02] Expand RSS sources where verified, keep non-feed signals as Future Work

**Selected Option:** Add verified security/release feeds (GitLab releases + security
releases, GitHub security category, Sonatype security tag, Project Zero, Unit 42) to
`config/sources.yaml`. Do **not** pretend IR pages, pricing HTML, or job boards are
RSS rows - they failed verification (403/HTML) or need scrapers + change detection.
Document a **production adapter roadmap** in README (IR/EDGAR, pricing diff, jobs APIs)
that still lands on the same normalize → classify → score pipeline.

**Alternatives Considered:**
- Invent / hardcode fake IR & careers URLs (rejected: assignment forbids mock URLs)
- Build pricing-diff + Greenhouse scrapers in the same day (rejected: scope / brittle)
- Skip security research entirely (rejected: assignment lists it, feeds exist)
- Force all brief source types into one RSS fetcher (rejected: wrong abstraction)

**Rationale:** Simplicity + honesty - extend the working ingest path only for URLs
`verify_feeds.py` accepts, ship different adapters later without rewriting scoring/UI.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "We didn't skip those source types on purpose - our pipeline is RSS/Atom. Security
> and release feeds we verified and turned on. Earnings IR blocked us with 403, and
> pricing/jobs are HTML change-detection problems - next we'd add dedicated adapters
> into the same normalize step, not invent feed URLs."

**JFrog Product Connection (If applicable):**
Xray/Curation already care about security advisory provenance, extra security feeds
feed the same "trusted upstream signal" mindset. Pricing/jobs adapters would be treated
like untrusted package metadata - isolate, validate, then promote.

---

## [2026-10-02] Ask Digest: comparison matrix grounding + capped session follow-ups

**Selected Option:** Inject the full curated `comparison.yaml` matrix into every Ask
prompt alongside top-6 keyword-retrieved news. Prompt instructs: answer **ONLY** from
those two sources and say so when neither covers the question (**prompt instruction,
not a technical guarantee**). Short transcript is re-sent each call from Streamlit
**session state** only (lost on refresh / New chat), hard cap **2 follow-ups**
(3 user turns total). Not model memory. Each follow-up **re-runs** retrieval over the
whole non-filtered digest using prior user questions + current question. If the matrix
exists but does not cover the question, there is **no** hard-coded empty UI state -
the model should say so under the prompt rule.

**Alternatives Considered:**
- Pass comparison into news *classification* scoring (rejected: biases daily scores;
  matrix is for product posture Q&A, not every RSS item)
- Unlimited chat / Gemini ChatSession memory (rejected: unbounded token cost)
- Persistent conversation table in SQLite (rejected: overkill for take-home demo)
- Matrix-only when keywords match capabilities (deferred: matrix is small enough to
  always include, simpler prompt contract)
- Hard-coded empty UI when matrix misses the question (rejected: matrix is almost
  always present; rely on the prompt “say so clearly” rule instead)

**Rationale:** Cost/Latency + Security - grounded product answers without inventing
cells, follow-up continuity for demos without runaway spend. Session transcript only.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Ask keyword-retrieves over the whole digest, always attaches our sourced comparison
> matrix, and tells Gemini to use only those - that's a prompt rule, not a sandbox.
> Follow-ups re-retrieve and resend a short session transcript twice, then New chat -
> nothing is stored as model memory."

**JFrog Product Connection (If applicable):**
A production CI assistant would treat comparison claims like curated catalog metadata
(AppTrust / trusted content) and gate tools via an MCP Registry before agents can call them.

---

## [2026-10-02] Database: Turso for shared demo, Postgres for real production

**Selected Option (this take-home):** Turso (hosted libSQL / SQLite-compatible) when
`TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` are set, otherwise local SQLite
(`data/ci_intel.db`). No seed/fake database.

**Selected Option (if this were a real production product):** Managed **PostgreSQL**
(e.g. AWS RDS, Cloud SQL, or Neon/Supabase Postgres) behind a small service API -
not a laptop SQLite file and not Turso as the long-term system of record.

**Alternatives Considered:**
- Stay on local SQLite only (rejected for multi-reviewer demos - each laptop diverges)
- Supabase/Neon Postgres already in the take-home (rejected for now: larger migration
  from our SQLite schema/repository in a ~2-day window)
- Turso forever in production (rejected: weaker fit for heavy concurrent writers,
  complex analytics, org backup/compliance/SSO expectations vs mature Postgres ops)

**Rationale:**
- **Take-home / shared demo:** Turso keeps the SQLite mental model and schema we already
  built, adds a free shared remote so every interviewer sees the same digest, weights,
  and feedback - minimal code change, easy to explain.
- **Real production:** Competitive-intel is multi-user, needs concurrent writes, richer
  querying, point-in-time backup, IAM, and usually sits next to other enterprise services.
  Managed Postgres is the default boring/correct choice, SQLite/Turso remain fine for
  edge caches or single-tenant appliances, not the primary shared CI warehouse.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "For the assignment I kept SQLite and added Turso so reviewers share one database
> without rewriting the data layer. If this shipped as a real product, I would move the
> system of record to managed Postgres for concurrency, backups, and operational maturity,
> and keep SQLite only where it still wins on simplicity."

**JFrog Product Connection (If applicable):**
Production data stores and app images would be scanned/promoted with Xray + AppTrust;
connection secrets belong in a secret manager, not in git.

---

## [2026-10-02] Community feeds (Reddit + HN) alongside official vendor RSS

**Selected Option:** Add verified community feeds (`r/devops` Atom, HN filtered via
hnrss) as `category: community` / `competitor: industry`. No Twitter/X (paid/fragile API).

**Alternatives Considered:** Vendor blogs only (misses market perception), Twitter/X API
(rejected: cost and auth complexity for a take-home).

**Rationale:** Official feeds = product facts, community feeds = sentiment/early signal.
Both are pulled the same way (httpx + feedparser) after `verify_feeds.py` checks the URL.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "We ingest official blogs for claims we can trust operationally, and Reddit/HN for
> how practitioners talk about the space - tagged as community so we never confuse
> sentiment with a sourced comparison-matrix claim."

**JFrog Product Connection (If applicable):**
Not directly, analogous to separating trusted provenance from noisy signals in Xray.

---

## [2026-10-02] Light RAG "Ask the Digest" now, embeddings later

**Selected Option:** Retrieve from the **whole** stored non-filtered digest by keyword
token overlap (+ slight relevance boost), take top **6**, attach the curated comparison
matrix, then call Gemini. **No embeddings / Vector DB.** Prompt says answer only from
retrieved news + matrix (instruction, not a guarantee). See also the Ask follow-ups
entry for session transcript / re-retrieve behavior.

**Alternatives Considered:** Embeddings + Chroma/Pinecone now (rejected: corpus is small;
listed as Future Work in the brief), plain chat without retrieval (rejected: invites
hallucination), recent-window-only retrieve (rejected: Ask should search the full digest
the UI can already show).

**Rationale:** Shows retrieve→augment→generate without over-engineering. When volume
grows (thousands of items, semantic queries), swap the retriever for embeddings.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Ask is intentional light RAG: keyword overlap over our SQLite digest, top six, plus
> the sourced matrix - no embeddings yet. The model is told to stay inside that context;
> we do not claim a technical hard block on outside knowledge."

**JFrog Product Connection (If applicable):**
Grounded answers with source links mirror "never trust unscanned/unknown provenance."

---

## [2026-10-02] Persistable weight overrides in app_settings

**Selected Option:** UI **Save weights** writes normalized weights JSON to
`app_settings` so all reviewers share the same ranking when using Turso.

**Alternatives Considered:** Session-only sliders (rejected: lost on refresh), write only
to `weights.yaml` (rejected: not shared across machines).

**Rationale:** Matches the lead-scoring story - humans can set a baseline, feedback loop
(Future Work) can later adjust automatically.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Sliders re-rank instantly from stored dimensions. Save writes the weights into the
> shared database so the panel all sees the same ordering."

**JFrog Product Connection (If applicable):**
Similar spirit to tunable policy packs that teams adjust without re-scanning everything.

---

## [2026-10-02] Seed DB committed, runtime DB gitignored

**Superseded by:** [2026-10-03] Removing the seed database

**Selected Option:** Commit `data/seed.db` (built by `scripts/seed_db.py`) for offline
demos, gitignore `data/ci_intel.db`. UI `resolve_db_path` prefers a non-empty
runtime DB, then falls back to seed.

**Alternatives Considered:** Always require a live pipeline before demo (rejected:
fragile if feeds/API fail during presentation), commit the runtime DB after every
cron run (rejected: noisy git history, merge conflicts).

**Rationale:** Demo reliability - a hiring panel must see Digest + Comparison even
when the network or Gemini is unavailable.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Seed data is a first-class demo artifact. Live runs write to a gitignored
> runtime DB, if that is empty, the UI opens seed.db so the story never depends
> on a perfect network day."

**JFrog Product Connection (If applicable):**
Not directly applicable, mirrors the idea of reproducible build artifacts for demos.

---

## [2026-10-02] Model ids in config (pipeline, fallback, ask)

**Selected Option:** Keep Gemini model **ids** only in `config/model.yaml`:
`pipeline_model` / `model_id` (primary classifier, e.g. `gemini-3.8-flash`),
`fallback_model` (quota / `--use-fallback-model`, e.g. `gemini-3.5-flash-lite`),
`ask_model` (Ask the Digest, e.g. `gemini-3.1-flash-lite`). Single
`GEMINI_API_KEY`. `provider:` is informational - the SDK path is Gemini-specific
today; multi-provider abstraction is Future Work.

**Alternatives Considered:** One shared model for pipeline + Ask (rejected: Ask
competes with cron for the same PerDay bucket), Pro models (billing), hardcoding
ids in Python (rejected: blocks id swaps), claiming full provider portability
already (rejected: dishonest - still `google-generativeai`).

**Rationale:** Cost/Latency - separate free-tier RPD pools and RPM spacing per
model id; YAML still owns which Gemini model each role uses.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Pipeline, fallback, and Ask each have their own model id in YAML so we can
> spend a stronger Flash on classification and a lighter one on chat, and fail
> over when PerDay hits. Swapping Gemini ids is one line; swapping vendors would
> need a thin provider layer we deliberately deferred."

**JFrog Product Connection (If applicable):**
Production images depending on `google-generativeai` would be scanned with Xray
before promotion; model choice is like picking a scanner policy pack per workload.

---

## [2026-10-02] Feedback table built now, learning engine is Future Work

**Selected Option:** SQLite `feedback` table (item id, original score, up/down,
optional rationale, timestamp) plus 👍/👎 + rationale in the Digest UI. No
automated weight-adjustment engine in v1.

**Alternatives Considered:** Shipping a learning loop in the take-home (rejected:
scope risk, hard to evaluate in two days), skipping feedback storage entirely
(rejected: loses the presentation story and future data).

**Rationale:** Scope discipline - capture the signal now, document the lead-
scoring-style loop as Future Work so the panel sees intentional maturity, not an
unfinished half-feature.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Operators can already leave thumbs and a short rationale. The learning engine
> that would turn that into automatic weight updates is deliberately Future Work-
> same pattern as a lead-scoring feedback loop."

**JFrog Product Connection (If applicable):**
Not applicable beyond the product-analogy of continuous policy tuning from user
signal (similar in spirit to refining curation/policy packs from outcomes).

---

## [2026-10-02] Comparison matrix as curated YAML (not LLM-generated)

**Selected Option:** `config/comparison.yaml` drives the Comparison tab. Every
cell has `claim`, `source_url`, and `quote`, or is forced to **Unknown** when the
source is missing. Service layer never invents claims from model memory.

**Alternatives Considered:** Asking the LLM to fill the matrix each run (rejected:
hallucination risk), scraping product pages live into the matrix (rejected:
fragile HTML, harder to audit for a take-home).

**Rationale:** Security / trust - competitive claims must be auditable. Curated
YAML with mandatory source links is the simplest honest approach at this scale.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "The comparison matrix is configuration, not generation. If we cannot cite an
> official page, we show Unknown. That is how we keep hallucinations out of the
> boardroom view."

**JFrog Product Connection (If applicable):**
Provenance of claims mirrors supply-chain provenance: trust comes from attested
sources, not from an opaque model answer.

---

## [2026-10-02] JFrog Medium feed instead of official blog RSS

**Selected Option:** Enable `https://medium.com/feed/@JFrog.com` as `jfrog_medium`
after verification showed `https://jfrog.com/blog/feed/` returning empty content
(HTTP 202). Documented in `config/sources.yaml`.

**Alternatives Considered:** Inventing a mock JFrog blog URL (forbidden);
scraping the HTML blog without a feed (rejected: more brittle, higher injection
surface), disabling JFrog self-ingestion (rejected: digest would miss own news).

**Rationale:** Reliability - only verified feeds ship, engineering judgment
documented rather than silent workarounds.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "We verified every feed. The official JFrog blog RSS was empty under automation,
> so we switched to the verified Medium publication feed and wrote that down in
> config and DECISIONS."

**JFrog Product Connection (If applicable):**
Not applicable.

---

## [2026-10-02] Scoring dimensions and default weights

**Selected Option:** Five LLM dimensions (1-5) with weights in `config/weights.yaml`
summing to 1.0: `jfrog_relevance` 0.30, `competitor_signal` 0.25,
`strategic_impact` 0.25, `freshness` 0.10, `market_visibility` 0.10. Weighted
total computed in `src/process/scoring.py`, dims stored in `dimension_scores`.

**Alternatives Considered:** Single LLM relevance score (opaque, not retunable);
equal weights across all dims (weaker signal on JFrog-direct news), embedding
similarity as the primary ranker (overkill at current volume).

**Rationale:** Transparency + cost - operators (and future feedback learning) can
retune ranking without re-calling Gemini. UI sliders normalize to sum 1.0.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "The model answers five narrow questions. Python applies configurable weights.
> That separation is what lets us demo weight tuning live and, later, learn from
> thumbs without burning tokens."

**JFrog Product Connection (If applicable):**
Multi-dimension scoring is analogous to Xray **contextual analysis**: severity
alone is incomplete-context (reachability, usage, business impact) changes
priority. Our dims play the same role for news ranking.

---

## [2026-10-02] Gemini structured JSON classification + daily pipeline

**Selected Option:** `google-generativeai` with Pydantic `ClassificationResult` as
`response_schema`, plus a thin `run_daily` orchestrator (fetch → dedupe → classify →
weighted score in code → SQLite). Prompt wraps RSS text in
`<<<UNTRUSTED_CONTENT>>>` … `<<<END_UNTRUSTED_CONTENT>>>`.

**Alternatives Considered:** LangChain/LlamaIndex orchestration (rejected: heavier
dependency surface for a linear pipeline), free-form text + regex parsing (rejected:
brittle), scoring entirely inside the LLM (rejected: weights must stay config-tunable).

**Rationale:** Cost/Simplicity/Security - structured output reduces parse failures;
`max_items_per_run` + rate-limit sleep caps spend, delimiters document prompt-injection
awareness without over-engineering a sandbox.

**How to Explain in an Interview (20-30 Seconds Verbal):**
> "Classification is one Gemini call per new item with a Pydantic JSON schema. Untrusted
> RSS text sits between explicit delimiters so injection attempts are treated as data.
> Dimension scores stay in the DB, weighted ranking is pure Python so we can retune
> weights without re-calling the model."

**JFrog Product Connection (If applicable):**
In production, dependency scanning for `google-generativeai` would sit behind JFrog
Xray/Curation before the pipeline image ships.

---

## [Setup] LLM provider: Google Gemini (free tier)

**Chosen:** Google Gemini via `GEMINI_API_KEY` in `.env`. Prefer a free Flash / Flash-Lite model
(e.g. `gemini-3.8-flash` or `gemini-3.5-flash-lite` for new projects), keep the model ID in config so
it can be swapped in one line.

**Alternatives considered:** OpenAI (paid after a small credit / no durable free API quota for this
workload), Anthropic Claude (similar paid-first API posture).

**Why:** Gemini still offers a usable free Developer API tier (no billing required for eligible Flash
models), which fits a ~2-day take-home with many classification/summarization calls. Rate limits are
per project (RPM / TPM / RPD) and should be read live in Google AI Studio, Pro models generally need
billing.

**Talking points (spoken, ~30 seconds):**
> "I picked Gemini for the free tier headroom so I can iterate on prompts without burning budget.
> The model name lives in config, so swapping providers later is a one-line change plus a different
> API key in `.env`."

**Possible JFrog product connection:** Not applicable here.

---

## [Planning] Weighted relevance scoring and a future feedback loop

**Chosen:** The LLM rates each item on several dimensions (1-5). The weighted score is computed in
code from weights in a config file. The UI exposes sliders to tune the weights. User feedback
(thumbs up/down plus an explanation) is stored in a table.

**Alternatives considered:** A single score straight from the LLM (inconsistent, not transparent);
fixed weights hard-coded in the source.

**Why:** Transparency (every score can be explained), low cost (changing weights needs no new LLM
call), and user control.

**Talking points (spoken, ~30 seconds):**
> "The LLM answers narrow questions and I compute the final score in code, so it is transparent and
> tunable. In the future users will not tune weights by hand: they will correct a score with a short
> explanation, and a learning loop will adjust the weights. The feedback groundwork is already built."

**Possible JFrog product connection:** Not applicable here.

---

## [Planning] MCP: not in the pipeline, optional bonus at the end

**Chosen:** Collection is plain code. A read-only MCP server over the database is added only if the
core is complete.

**Alternatives considered:** Using MCP for source collection.

**Why:** MCP adds complexity to RSS collection with no benefit. The real value is letting the team
query the intelligence base in natural language from the tools they already use.

**Talking points (spoken, ~30 seconds):**
> "I did not use MCP where plain code is enough. As a next step I would expose the database as a
> read-only MCP server so the team can ask questions from the tools they already use."

**Possible JFrog product connection:** JFrog MCP Registry, to vet MCP servers before they are used in
a production environment.

---

## [Planning] Tracked competitors and sources

**Chosen:** Core: Sonatype, GitHub, GitLab, Snyk. Secondary: Cloudsmith, Harness. The list lives in config.

**Alternatives considered:** Tracking the AWS/Google registries as companies.

**Why:** Sonatype is the most direct competitor, the others consistently appear in competitor lists.
Cloud registries are pricing pressure rather than competitors to monitor daily. The sources were
third-party aggregators, so this is a documented judgment call.

**Talking points (spoken, ~30 seconds):**
> "I picked four core competitors based on product overlap and kept the list in config so the team can
> extend it. Every claim in the system comes with a source, to prevent hallucinations."

**Possible JFrog product connection:** Not applicable here.

---

## [Planning] Streamlit UI in a JFrog-like style

**Chosen:** Streamlit with a custom theme and CSS (dark navy #070B19 background, green #40BE46 accent,
Open Sans).

**Alternatives considered:** A custom React front end.

**Why:** Faster to build and simpler to explain, React would give a more exact finish at the cost of
time and complexity. No public JFrog component library was found, so components imitate the style.

**Talking points (spoken, ~30 seconds):**
> "I chose Streamlit to invest in the logic rather than UI plumbing, and themed it after JFrog's visual
> language: dark background, green accents, Open Sans. I could not find a public component library, so
> I recreated the style."

**Possible JFrog product connection:** Not applicable here.

---

<!--
Template for a new entry - copy and fill in for each decision:

## [Date/Stage] Short decision title

**Chosen:**

**Alternatives considered:**

**Why:**

**Talking points (spoken, ~30 seconds):**
>

**Possible JFrog product connection (if relevant):**

-->
