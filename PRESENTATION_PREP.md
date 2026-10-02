# Presentation Prep — CI Intel Tool

Materials for a ~7–10 minute panel presentation (technical audience). Aligns with the
running codebase: Gemini classification, code-side weights, curated comparison YAML,
seed DB, Streamlit Digest + Comparison.

---

## 7–10 minute script

### 1. Problem (≈1 min)

Competitive intelligence for a platform company like JFrog is noisy: blogs, release
notes, industry outlets, status feeds. Analysts need a **daily digest ranked by what
matters to JFrog**, plus a **fair comparison view** that never invents product claims.

The trap with GenAI here is hallucination and opaque scores. If every claim cannot be
traced to a source, the tool is not usable in a sales or strategy conversation.

### 2. Strategy (≈1.5 min)

Keep the system small and honest:

1. **Ingest** verified RSS/Atom feeds for JFrog + Sonatype, GitHub, GitLab, Snyk (plus
   industry context). Secondary competitors exist in config but are disabled.
2. **Dedupe** by URL and content hash.
3. **Classify** with Gemini: short summary, category, and five 1–5 dimension scores.
4. **Score in code** using weights from YAML — retunable without re-querying the model.
5. **Store** dimensions, news, feedback, and run history in SQLite.
6. **Present** in Streamlit: Digest (weights, thumbs, Run Now) and Comparison (sourced
   matrix).

Deliberate non-goals for Stage 1: vector DB, Slack, MCP server, automated weight learning.

### 3. Architecture (≈2.5 min)

Walk the flow on a whiteboard or one slide:

```
Feeds → normalize → dedupe → Gemini (structured JSON) → weighted_score() → SQLite
                                                                    ↓
                                              Streamlit ← services (digest / comparison / feedback)
```

Call out three engineering choices:

- **Config over hardcoding:** competitors, sources, weights, model id, comparison cells.
- **Service layer separate from UI:** same functions can later back a read-only MCP server.
- **Cost guards:** `max_items_per_run` (30), timeouts, excerpt caps, rate-limit sleep.

Mention daily automation: GitHub Actions cron at 06:00 UTC with `GEMINI_API_KEY` secret,
artifact upload of the DB (no forced git commit from CI).

### 4. Key decisions (≈2.5 min)

Spend time on the decisions that show judgment:

| Topic | One-liner |
|-------|-----------|
| Gemini Flash | Free-tier iteration budget; model id only in `config/model.yaml` (`gemini-3.8-flash`) |
| Weights in code | Dims stored; sliders re-rank live; future learning loop can adjust weights |
| Comparison YAML | Never LLM memory; Unknown if no official source |
| JFrog Medium feed | Official blog RSS empty under automation; verified alternate documented |
| Feedback now / learn later | Table + 👍/👎 built; automated weight engine is Future Work (lead-scoring analogy) |
| Seed DB | Offline demo resilience when network or API fails |

**Close the section with the presentation point:**

> Today people can move sliders. Tomorrow they should not have to. They will correct a
> score with a short rationale, and a feedback loop will adjust weights automatically—
> like lead scoring. We already store the signal.

### 5. Wrap (≈1 min)

Security in one breath: untrusted content delimiters, no secrets in git, source-linked
claims, cost caps; production would add Xray/Curation on dependencies and MCP Registry
for any future MCP tools.

Invite questions. Point reviewers to README + DECISIONS.md.

---

## Live demo outline

**Prep (before the panel):**

```bash
source .venv/bin/activate
python scripts/seed_db.py
streamlit run src/ui/app.py
```

Optional: confirm feeds with `python scripts/verify_feeds.py` and a dry-run with
`python -m src.pipeline.run_daily --dry-run` if the room has network.

**Demo path (~4–5 minutes):**

1. **Open Daily Digest** — point at KPI row, sorted cards, competitor badges, score rings.
2. **Move a weight slider** (e.g. raise `jfrog_relevance`) — ranking changes without LLM.
3. **Leave 👍/👎 + rationale** on one item — show feedback is persisted (groundwork for learning).
4. **Pipeline run history** — seed/cron/manual runs prove automation story.
5. **Comparison tab** — open a sourced claim (link + quote); find an **Unknown** cell and
   explain why that is better than inventing text.
6. **(Optional) Run Now** — only if API key + network are healthy; otherwise skip and say so.

### Contingency plan

| Failure | What to do |
|---------|------------|
| No internet / feed down | Stay on `data/seed.db`; do not click Run Now |
| Missing / invalid `GEMINI_API_KEY` | Skip live pipeline; show dry-run output from earlier or README |
| Streamlit CSS oddities | Note pinned `streamlit==1.39.0`; functional demo still works |
| Empty digest | Re-run `python scripts/seed_db.py` and refresh |

**Line to say:** “The seed database is intentional demo insurance—same principle as a
pre-populated catalog for a customer demo when a dependency is offline.”

---

## Anticipated panel questions (8–10)

**Q1. Why Gemini instead of OpenAI or Claude?**  
A. Free Developer API tier for Flash models fits a two-day take-home with many
classification calls. Model id stays in config so a paid provider swap is one line plus
a new env key.

**Q2. Why not let the LLM return the final relevance score?**  
A. A single opaque score cannot be retuned without re-calling the model. Storing five
dimensions and weighting in Python makes ranking transparent, testable, and feedback-
ready.

**Q3. How do you prevent hallucinations in the comparison matrix?**  
A. The matrix is curated YAML from official product pages. Missing sources become
Unknown. The LLM never fills those cells.

**Q4. What is prompt-injection risk here, and what did you do?**  
A. RSS text is untrusted. We wrap it in explicit `UNTRUSTED_CONTENT` delimiters and
instruct the model to ignore embedded instructions. That is defense-in-depth, not a
perfect sandbox.

**Q5. How do you control cost?**  
A. `max_items_per_run`, timeouts, excerpt length, and sleep between calls in
`model.yaml`. Dry-run validates fetch/dedupe without tokens. Per-item classify failures
do not abort the whole run.

**Q6. Why Streamlit instead of React?**  
A. Faster path to a working UI for logic-heavy work. Custom CSS emulates JFrog look-
and-feel; we pin Streamlit because tab/button CSS is fragile across versions. No official
public JFrog component library was used.

**Q7. How would this scale if volume grows 10×?**  
A. First: raise ingestion selectivity and keep cost caps. Later: embeddings/vector search
for near-dupe and semantic retrieval, eval suite for classification quality, optional MCP
for analyst query. Architecture already separates services from UI.

**Q8. What is the feedback loop you keep mentioning?**  
A. Like lead scoring: humans correct outcomes with a short rationale; a learning job
adjusts weights (and maybe prompts). We store votes now; the learning engine is Future
Work by design.

**Q9. Why Medium for JFrog instead of the official blog feed?**  
A. Verification showed the official feed empty (HTTP 202). We documented the Medium
publication feed as the working alternate rather than inventing a mock URL.

**Q10. Where does JFrog’s product line connect to this design?**  
A. Multi-dimension news scoring is analogous to Xray contextual analysis (severity alone
mis-prioritizes). Dependency risk on the pipeline image → Xray/Curation. Future MCP
tools → MCP Registry before enterprise use.

---

## What I would improve with more time (3–4 honest points)

1. **Feedback → weight learning:** fit a small, auditable updater (constrained deltas,
   human review) from stored 👍/👎 + rationales.
2. **Eval suite:** golden set of news snippets with expected dims/categories; regression
   gate before prompt or model changes.
3. **Read-only MCP server:** expose `search_news`, `get_comparison`, `get_digest` over the
   existing service layer for Cursor / Claude Desktop.
4. **Richer ingestion:** enable secondary competitors when verified; optional changelog
   HTML parsers with the same untrusted-content isolation; Slack digest of top-N scores.

---

## JFrog product connection points

| Product / concept | Connection in this project |
|-------------------|----------------------------|
| **Xray contextual analysis** | Dimension scores vs a single severity-like number: context changes priority (reachability ↔ strategic_impact / jfrog_relevance) |
| **Curation** | Blocking bad packages ≈ refusing unsourced competitive claims (Unknown > invented text); policy packs ↔ tunable weights |
| **MCP Registry** | If we ship a custom CI-intel MCP later, treat it as a vetted enterprise asset—register and review before analysts attach it |
| **Artifactory / build info (Stage 2 mindset)** | Cron + artifact upload of the DB is a lightweight “publish the output”; Stage 2 Docker/Artifactory deepens that story |

Use these as bridges in Q&A, not as forced product plugs.
