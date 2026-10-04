#!/usr/bin/env python3
"""A/B/C compare scoring rubrics + models on fixed stored titles (no DB writes).

Configurations (same 13 items, batches of 5):
  A. gemini-3.8-flash + LEGACY rubric  → 3 live calls
  B. gemini-3.8-flash + CURRENT rubric → 3 live calls
  C. gemini-3.5-flash-lite + CURRENT   → 3 live calls

Groups:
  - targets (3): items previously over-scored / under discussion
  - low (5): should stay LOW under the new rubric
  - high (5): should stay HIGH under the new rubric

Usage (from repo root, when quota allows):
  PYTHONPATH=. python scripts/compare_models.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_model_config, load_weights
from src.db.connection import get_connection, init_db, turso_configured
from src.db.repository import Repository
from src.ingest.normalize import NormalizedEntry
from src.process.llm_classify import (
    LlmUsageGuard,
    batch_item_id,
    chunk_entries,
    classify_entries_batch,
)
from src.process.llm_quota import (
    DailyQuotaError,
    format_reset_times,
    model_min_interval_seconds,
    resolve_pipeline_model,
)
from src.process.llm_rate_limit import configure_llm_interval
from src.process.scoring import weighted_score

# Fixed sample — match by title substring (case-insensitive).
TARGET_TITLES = [
    "The Open-Source Artifactory Alternative",
    "Evo ADS Govern Agent Behavior Goes GA",
    "Trust Docker for the agents you don't",
]
LOW_TITLES = [
    "What's new in Git 2.56.0?",
    "What is Interactive Application Security Testing (IAST)?",
    "AI is changing developer work. Here are three skills to strengthen.",
    "Selected models in GitHub Copilot deprecated",
    "GPT-6 Sol on Codex: average scores, quarter the cost",
]
HIGH_TITLES = [
    "Sckit Supply Chain Worm Hits MemTensor npm & PyPi scopes",
    "GitLab and Claude Code: Fast, compliant AI",
    "Securing the Financial Frontier: How Capital One Uses Socket for Open Source Security",
    "Pretty Themes, Hidden Loaders: GlassWorm-Linked Extensions",
    "Agents Pick Dependencies, DoWI 8430.01 Holds You Accountable",
]

# (label, model_id resolver key, legacy_rubric)
# model_id is filled at runtime from config.
CONFIGS = (
    ("A", "primary", True),
    ("B", "primary", False),
    ("C", "fallback", False),
)


def _norm(title: str) -> str:
    # Normalize curly/smart quotes so fixed needles match stored titles.
    t = (title or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(t.split())


def _find_row(rows: list[dict], needle: str) -> dict | None:
    n = _norm(needle)
    for row in rows:
        if n in _norm(str(row.get("title") or "")):
            return row
    return None


def _pick_group(rows: list[dict], needles: list[str], label: str) -> list[dict]:
    picked: list[dict] = []
    missing: list[str] = []
    for needle in needles:
        row = _find_row(rows, needle)
        if row is None:
            missing.append(needle)
        else:
            picked.append(row)
    if missing:
        print(f"WARNING: {label} missing {len(missing)} title(s):", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
    return picked


def _to_entry(row: dict) -> NormalizedEntry:
    return NormalizedEntry(
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        published_at=row.get("published_at"),
        raw_excerpt=str(row.get("raw_excerpt") or ""),
        source_id=str(row.get("source_id") or ""),
        competitor=str(row.get("competitor") or "industry"),
        content_hash=str(row.get("content_hash") or row.get("id") or ""),
    )


def _short_title(title: str, width: int = 48) -> str:
    t = " ".join((title or "").split())
    return t if len(t) <= width else t[: width - 1] + "…"


def _score_entries(
    entries: list[NormalizedEntry],
    *,
    model_id: str,
    cfg: dict,
    weights: dict[str, float],
    legacy_rubric: bool,
    finished: list[str],
    config_label: str,
    usage_guard: LlmUsageGuard,
) -> dict[str, float]:
    """Classify in batches of 5. Returns content_hash -> weighted total.

    News rows are not written; ``llm_usage`` / ``llm_model_blocks`` are updated
    via ``usage_guard`` so compare runs respect hard PerDay cooldowns.
    """
    # Keep the guard pointed at the model we are about to call.
    usage_guard.model_id = model_id
    configure_llm_interval(model_min_interval_seconds(cfg, model_id))
    out: dict[str, float] = {}
    batches = list(chunk_entries(entries, 5))
    for i, batch in enumerate(batches, start=1):
        ids = [batch_item_id(e) for e in batch]
        try:
            accepted, missing, _retries = classify_entries_batch(
                batch,
                model_config=cfg,
                item_ids=ids,
                model_id_override=model_id,
                usage_guard=usage_guard,
                legacy_rubric=legacy_rubric,
            )
        except DailyQuotaError as exc:
            until = exc.blocked_until
            if until is not None:
                utc_label, israel_label = format_reset_times(until)
                print(
                    f"Model {model_id} blocked until UTC={utc_label} "
                    f"Israel={israel_label}",
                    file=sys.stderr,
                )
            print(
                f"STOPPED on quota during config {config_label} "
                f"batch {i}/{len(batches)}. Finished configs: {finished or ['(none)']}",
                file=sys.stderr,
            )
            raise
        if missing:
            tag = "LEGACY" if legacy_rubric else "CURRENT"
            print(
                f"WARNING: {config_label} ({tag}) missing ids after batch: {missing}",
                file=sys.stderr,
            )
        for item_id, result in accepted.items():
            out[item_id] = weighted_score(result.dimension_dict(), weights)
    return out


def _scores_for_group(
    rows: list[dict],
    scores_by_config: dict[str, dict[str, float]],
) -> dict[str, list[float]]:
    """Collect per-config score lists for a group (skip missing)."""
    collected: dict[str, list[float]] = {k: [] for k in scores_by_config}
    for row in rows:
        iid = batch_item_id(_to_entry(row))
        if all(iid in scores_by_config[k] for k in scores_by_config):
            for k in scores_by_config:
                collected[k].append(scores_by_config[k][iid])
    return collected


def _print_group_table(
    label: str,
    rows: list[dict],
    scores_by_config: dict[str, dict[str, float]],
) -> None:
    print(f"=== {label} ({len(rows)} items) ===")
    print(f"{'title':<50} {'A':>6} {'B':>6} {'C':>6}")
    print("-" * 72)
    for row in rows:
        entry = _to_entry(row)
        iid = batch_item_id(entry)
        title = _short_title(str(row.get("title") or ""), 48)
        vals = []
        for key in ("A", "B", "C"):
            v = scores_by_config.get(key, {}).get(iid)
            vals.append(f"{v:6.2f}" if v is not None else f"{'?':>6}")
        print(f"{title:<50} {vals[0]} {vals[1]} {vals[2]}")
    print()


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _ge3(vals: list[float]) -> int:
    return sum(1 for v in vals if v >= 3.0)


def _top_n_ids(
    rows: list[dict],
    scores: dict[str, float],
    n: int = 3,
) -> list[str]:
    ranked: list[tuple[float, str]] = []
    for row in rows:
        iid = batch_item_id(_to_entry(row))
        if iid in scores:
            ranked.append((scores[iid], iid))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    return [iid for _, iid in ranked[:n]]


def main() -> int:
    if not turso_configured():
        print("Turso is not configured; need stored items to compare.", file=sys.stderr)
        return 2
    init_db()
    conn = get_connection()
    try:
        repo = Repository(conn)
        rows = repo.list_news_with_scores()

        targets = _pick_group(rows, TARGET_TITLES, "targets")
        lows = _pick_group(rows, LOW_TITLES, "low")
        highs = _pick_group(rows, HIGH_TITLES, "high")
        groups = [
            ("targets", targets),
            ("expected-low", lows),
            ("expected-high", highs),
        ]
        all_rows = targets + lows + highs
        if len(all_rows) != 13:
            print(
                f"Need all 13 fixed titles in storage; found {len(all_rows)}. Aborting.",
                file=sys.stderr,
            )
            return 2

        cfg = load_model_config()
        weights = load_weights()
        primary = resolve_pipeline_model(cfg, use_fallback=False)
        fallback = resolve_pipeline_model(cfg, use_fallback=True)
        model_for = {"primary": primary, "fallback": fallback}

        entries = [_to_entry(r) for r in all_rows]
        n_batches = len(list(chunk_entries(entries, 5)))

        print("Configs on 13 fixed items (batches of 5), in-memory only — no DB writes")
        print(f"  A: {primary} + LEGACY rubric  (~{n_batches} calls)")
        print(
            f"  B: {primary} + CURRENT rubric "
            f"(rubric_version={cfg.get('rubric_version')!r})  (~{n_batches} calls)"
        )
        print(f"  C: {fallback} + CURRENT rubric  (~{n_batches} calls)")
        print(
            f"  min delay: {primary}={model_min_interval_seconds(cfg, primary)}s, "
            f"{fallback}={model_min_interval_seconds(cfg, fallback)}s"
        )
        print()

        scores_by_config: dict[str, dict[str, float]] = {}
        finished: list[str] = []
        # Shared guard: increments llm_usage and honors llm_model_blocks.
        usage_guard = LlmUsageGuard(
            repo, cfg, purpose="compare", model_id=primary
        )

        for label, model_key, legacy in CONFIGS:
            model_id = model_for[model_key]
            rubric = "LEGACY" if legacy else "CURRENT"
            print(f"Running config {label}: {model_id} / {rubric} ...", flush=True)
            try:
                scores_by_config[label] = _score_entries(
                    entries,
                    model_id=model_id,
                    cfg=cfg,
                    weights=weights,
                    legacy_rubric=legacy,
                    finished=finished,
                    config_label=label,
                    usage_guard=usage_guard,
                )
            except DailyQuotaError as exc:
                print(
                    f"STOPPED: {model_id} hit daily PerDay quota during config {label} "
                    f"(hint={exc.retry_hint!r}).",
                    file=sys.stderr,
                )
                print(f"Finished configs before stop: {finished or ['(none)']}")
                if scores_by_config:
                    print("Partial scores available for:", ", ".join(scores_by_config))
                return 3
            finished.append(label)
            print(f"  done {label} ({len(scores_by_config[label])} scores)", flush=True)

        print()
        for label, group_rows in groups:
            _print_group_table(label, group_rows, scores_by_config)

        print("=== group means & >=3.0 counts ===")
        print(
            f"{'group':<16} {'mean_A':>7} {'mean_B':>7} {'mean_C':>7} "
            f"{'>=3 A':>7} {'>=3 B':>7} {'>=3 C':>7}"
        )
        print("-" * 66)
        for label, group_rows in groups:
            collected = _scores_for_group(group_rows, scores_by_config)
            n = len(collected["A"])
            print(
                f"{label:<16} {_mean(collected['A']):7.3f} {_mean(collected['B']):7.3f} "
                f"{_mean(collected['C']):7.3f} "
                f"{_ge3(collected['A']):>3}/{n:<3} {_ge3(collected['B']):>3}/{n:<3} "
                f"{_ge3(collected['C']):>3}/{n:<3}"
            )

        # B vs C agreement metrics across all 13 items
        print()
        print("=== B vs C (CURRENT rubric: primary vs flash-lite) ===")
        abs_diffs: list[float] = []
        large_diff_rows: list[tuple[str, float, float, float]] = []
        for row in all_rows:
            iid = batch_item_id(_to_entry(row))
            b = scores_by_config["B"].get(iid)
            c = scores_by_config["C"].get(iid)
            if b is None or c is None:
                continue
            diff = abs(b - c)
            abs_diffs.append(diff)
            if diff >= 0.8:
                large_diff_rows.append(
                    (_short_title(str(row.get("title") or ""), 48), b, c, b - c)
                )

        mad = _mean(abs_diffs)
        top_b = set(_top_n_ids(all_rows, scores_by_config["B"], 3))
        top_c = set(_top_n_ids(all_rows, scores_by_config["C"], 3))
        overlap = len(top_b & top_c)
        print(f"mean |B-C| = {mad:.3f}  (n={len(abs_diffs)})")
        print(f"top-3 overlap B∩C = {overlap}/3")

        if large_diff_rows:
            print()
            print("items where |B-C| >= 0.8:")
            print(f"{'title':<50} {'B':>6} {'C':>6} {'B-C':>7}")
            print("-" * 72)
            for title, b, c, signed in large_diff_rows:
                print(f"{title:<50} {b:6.2f} {c:6.2f} {signed:+7.2f}")
        else:
            print("No items with |B-C| >= 0.8")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
