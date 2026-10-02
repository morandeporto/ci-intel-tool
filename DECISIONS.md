# Decisions Log

A running record of architectural decisions made during the project, newest first.
Maintained as decisions are made (see `.cursorrules`).

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

**Why:** Sonatype is the most direct competitor; the others consistently appear in competitor lists.
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

**Why:** Faster to build and simpler to explain; React would give a more exact finish at the cost of
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
