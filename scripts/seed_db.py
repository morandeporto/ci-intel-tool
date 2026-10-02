#!/usr/bin/env python3
"""Build an offline demo database at data/seed.db (no network / LLM calls).

Idempotent: deletes and recreates seed.db on every run so demos stay consistent.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# Allow `python scripts/seed_db.py` from repo root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import DATA_DIR, load_weights
from src.db.connection import SEED_DB_PATH, get_connection, init_db
from src.db.models import DimensionScores, NewsItem
from src.db.repository import Repository
from src.process.scoring import weighted_score

MODEL_ID = "seed/offline-v1"


def _utc(days_ago: float, hour: int = 12) -> str:
    base = datetime.now(timezone.utc).replace(microsecond=0, minute=0, second=0, hour=hour)
    return (base - timedelta(days=days_ago)).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Realistic sample items spanning several days across tracked competitors.
SEED_NEWS: list[dict] = [
    {
        "title": "JFrog expands Curation policies for malicious package blocking",
        "url": "https://jfrog.com/blog/curation-malicious-packages-seed-demo/",
        "source_id": "jfrog_blog",
        "competitor": "jfrog",
        "days_ago": 0.2,
        "summary": "JFrog details new Curation controls that block known-malicious packages before they enter the SDLC.",
        "category": "product",
        "raw_excerpt": "Curation helps organizations stop malicious open source before it reaches developers.",
        "dims": {"jfrog_relevance": 5, "competitor_signal": 2, "strategic_impact": 5, "freshness": 5, "market_visibility": 4},
    },
    {
        "title": "Sonatype Nexus Repository 3.x release highlights",
        "url": "https://www.sonatype.com/blog/nexus-repository-release-seed-demo",
        "source_id": "sonatype_blog",
        "competitor": "sonatype",
        "days_ago": 0.5,
        "summary": "Sonatype ships repository performance and proxy improvements aimed at large artifact estates.",
        "category": "release",
        "raw_excerpt": "This release focuses on proxy performance and repository health insights.",
        "dims": {"jfrog_relevance": 4, "competitor_signal": 5, "strategic_impact": 3, "freshness": 5, "market_visibility": 3},
    },
    {
        "title": "GitHub announces Packages improvements for enterprise registries",
        "url": "https://github.blog/packages-enterprise-seed-demo/",
        "source_id": "github_blog",
        "competitor": "github",
        "days_ago": 1.0,
        "summary": "GitHub Packages adds enterprise controls that tighten coupling between source and package workflows.",
        "category": "product",
        "raw_excerpt": "Publish and consume packages alongside your code with stronger org policies.",
        "dims": {"jfrog_relevance": 4, "competitor_signal": 5, "strategic_impact": 4, "freshness": 4, "market_visibility": 5},
    },
    {
        "title": "GitLab Secure stage adds dependency scanning refinements",
        "url": "https://about.gitlab.com/blog/dependency-scanning-seed-demo/",
        "source_id": "gitlab_blog",
        "competitor": "gitlab",
        "days_ago": 1.5,
        "summary": "GitLab improves Dependency Scanning signal quality inside the Secure stage of CI pipelines.",
        "category": "security",
        "raw_excerpt": "Analyze dependencies for known vulnerabilities without leaving the workflow.",
        "dims": {"jfrog_relevance": 3, "competitor_signal": 4, "strategic_impact": 4, "freshness": 4, "market_visibility": 3},
    },
    {
        "title": "Snyk Open Source adds automated fix PRs for popular ecosystems",
        "url": "https://snyk.io/blog/automated-fix-prs-seed-demo/",
        "source_id": "snyk_blog",
        "competitor": "snyk",
        "days_ago": 2.0,
        "summary": "Snyk expands automated remediation PRs, reinforcing its developer-first SCA position.",
        "category": "security",
        "raw_excerpt": "Find and automatically fix vulnerabilities in open source dependencies.",
        "dims": {"jfrog_relevance": 3, "competitor_signal": 5, "strategic_impact": 3, "freshness": 4, "market_visibility": 4},
    },
    {
        "title": "Industry roundup: software supply chain risk remains a board topic",
        "url": "https://devops.com/supply-chain-risk-board-seed-demo/",
        "source_id": "devops_com",
        "competitor": "industry",
        "days_ago": 2.3,
        "summary": "Analysts note continued executive attention on provenance, SCA, and trusted distribution.",
        "category": "market",
        "raw_excerpt": "Boards are asking for measurable supply-chain risk reduction, not more dashboards.",
        "dims": {"jfrog_relevance": 4, "competitor_signal": 2, "strategic_impact": 4, "freshness": 3, "market_visibility": 5},
    },
    {
        "title": "JFrog Artifactory adds ML model package ergonomics",
        "url": "https://jfrog.com/blog/ml-model-packages-seed-demo/",
        "source_id": "jfrog_blog",
        "competitor": "jfrog",
        "days_ago": 3.0,
        "summary": "Artifactory improvements make it easier to version and promote ML model artifacts with binaries.",
        "category": "product",
        "raw_excerpt": "Manage and secure ML models alongside software binaries.",
        "dims": {"jfrog_relevance": 5, "competitor_signal": 2, "strategic_impact": 5, "freshness": 3, "market_visibility": 3},
    },
    {
        "title": "Sonatype Lifecycle policy packs update for critical CVEs",
        "url": "https://www.sonatype.com/blog/lifecycle-policy-packs-seed-demo",
        "source_id": "sonatype_blog",
        "competitor": "sonatype",
        "days_ago": 3.5,
        "summary": "Updated policy packs help teams enforce faster response to high-severity open-source CVEs.",
        "category": "security",
        "raw_excerpt": "Automated open source governance and security for enterprise pipelines.",
        "dims": {"jfrog_relevance": 3, "competitor_signal": 4, "strategic_impact": 3, "freshness": 3, "market_visibility": 3},
    },
    {
        "title": "GitHub Actions marketplace sees surge in security workflows",
        "url": "https://github.blog/actions-security-workflows-seed-demo/",
        "source_id": "github_blog",
        "competitor": "github",
        "days_ago": 4.0,
        "summary": "More organizations standardize security scans as reusable Actions in CI.",
        "category": "market",
        "raw_excerpt": "Automate your workflow from idea to production with reusable security Actions.",
        "dims": {"jfrog_relevance": 3, "competitor_signal": 4, "strategic_impact": 3, "freshness": 2, "market_visibility": 4},
    },
    {
        "title": "GitLab quarterly update highlights DevSecOps platform traction",
        "url": "https://about.gitlab.com/blog/quarterly-update-seed-demo/",
        "source_id": "gitlab_ir",
        "competitor": "gitlab",
        "days_ago": 4.5,
        "summary": "Public commentary emphasizes consolidated DevSecOps tooling versus point solutions.",
        "category": "financial",
        "raw_excerpt": "Customers continue to consolidate security and delivery into a single platform.",
        "dims": {"jfrog_relevance": 3, "competitor_signal": 4, "strategic_impact": 4, "freshness": 2, "market_visibility": 4},
    },
    {
        "title": "Snyk Container scanning expands base-image recommendations",
        "url": "https://snyk.io/blog/container-base-image-seed-demo/",
        "source_id": "snyk_blog",
        "competitor": "snyk",
        "days_ago": 5.0,
        "summary": "Snyk improves guidance for safer base images, competing in the container security narrative.",
        "category": "security",
        "raw_excerpt": "Developer-first security across code, open source, containers, and IaC.",
        "dims": {"jfrog_relevance": 2, "competitor_signal": 4, "strategic_impact": 3, "freshness": 2, "market_visibility": 3},
    },
    {
        "title": "Pricing watch: registry platform bundling pressure continues",
        "url": "https://thenewstack.io/registry-bundling-seed-demo/",
        "source_id": "thenewstack",
        "competitor": "industry",
        "days_ago": 5.5,
        "summary": "Coverage notes that cloud registries and platform bundles remain contextual pricing pressure for specialized binary managers.",
        "category": "pricing",
        "raw_excerpt": "AWS ECR and GCP Artifact Registry remain contextual background, not discrete daily competitors.",
        "dims": {"jfrog_relevance": 4, "competitor_signal": 2, "strategic_impact": 3, "freshness": 2, "market_visibility": 3},
    },
]


def seed(db_path: Path = SEED_DB_PATH) -> dict[str, int]:
    """Rebuild seed.db from scratch. Returns counts for verification."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    init_db(db_path)
    weights = load_weights()
    conn = get_connection(db_path)
    repo = Repository(conn)

    # Two historical pipeline runs + one "seed" run that owns the news rows.
    older_run = repo.start_run("cron")
    repo.finish_run(
        older_run,
        status="success",
        items_fetched=18,
        items_new=6,
        items_scored=6,
    )
    # Backdate the older run for a realistic history panel.
    older_started = _utc(3, hour=6)
    older_finished = _utc(3, hour=6)
    conn.execute(
        "UPDATE pipeline_runs SET started_at = ?, finished_at = ? WHERE id = ?",
        (older_started, older_finished, older_run),
    )
    conn.commit()

    mid_run = repo.start_run("cron")
    repo.finish_run(
        mid_run,
        status="partial",
        items_fetched=20,
        items_new=4,
        items_scored=3,
        error_message="One feed timed out (simulated seed).",
    )
    mid_started = _utc(1, hour=6)
    conn.execute(
        "UPDATE pipeline_runs SET started_at = ?, finished_at = ? WHERE id = ?",
        (mid_started, mid_started, mid_run),
    )
    conn.commit()

    seed_run = repo.start_run("seed")

    news_count = 0
    for entry in SEED_NEWS:
        item_id = str(uuid4())
        published = _utc(entry["days_ago"], hour=10)
        ingested = _utc(entry["days_ago"], hour=11)
        dims = entry["dims"]
        score = weighted_score(dims, weights)
        item = NewsItem(
            id=item_id,
            title=entry["title"],
            url=entry["url"],
            source_id=entry["source_id"],
            competitor=entry["competitor"],
            published_at=published,
            ingested_at=ingested,
            summary=entry["summary"],
            category=entry["category"],
            raw_excerpt=entry["raw_excerpt"],
            content_hash=_hash(entry["url"] + entry["title"]),
            relevance_score=score,
            run_id=seed_run,
        )
        repo.upsert_news_item(item)
        repo.save_dimension_scores(
            item_id,
            DimensionScores.from_mapping(dims, model_id=MODEL_ID, scored_at=ingested),
        )
        news_count += 1

    repo.finish_run(
        seed_run,
        status="success",
        items_fetched=news_count,
        items_new=news_count,
        items_scored=news_count,
    )

    # Sample feedback for the feedback-loop demo (learning engine = Future Work).
    items = repo.list_news_with_scores()
    feedback_count = 0
    if len(items) >= 2:
        repo.add_feedback(
            items[0]["id"],
            float(items[0]["relevance_score"] or 0),
            "up",
            "High strategic value — Curation narrative matches our win themes.",
        )
        repo.add_feedback(
            items[1]["id"],
            float(items[1]["relevance_score"] or 0),
            "down",
            "Routine release notes; overweighted competitor_signal.",
        )
        feedback_count = 2

    conn.close()
    return {
        "news_items": news_count,
        "dimension_scores": news_count,
        "pipeline_runs": 3,
        "feedback": feedback_count,
    }


def main() -> None:
    counts = seed()
    print(f"Created {SEED_DB_PATH}")
    for key, value in counts.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
