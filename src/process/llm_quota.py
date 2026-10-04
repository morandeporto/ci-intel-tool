"""Gemini daily-quota detection and soft per-model usage budgeting.

Hard stop signal: API error whose quota id/name contains "PerDay", or repeated
429s after retries are exhausted (avoids retry loops). Soft budget: llm_usage
counters compared to model_daily_limits in config - advisory headroom only.

When a hard PerDay error includes a retry hint (e.g. "Please retry in 18h55m33s"),
we persist ``blocked_until = now + hint + safety margin`` in ``llm_model_blocks``
so pipeline / Ask / scripts skip the API until that time. Observed reset times are
logged in UTC and Asia/Jerusalem to help tune the nightly retry cron — do NOT
hardcode a reset hour into application logic.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

Purpose = Literal["pipeline", "ask", "rescore", "compare"]

# Extra cooldown after the provider retry hint so the first post-reset call
# is less likely to race the free-tier rollover.
BLOCK_SAFETY_MARGIN_SECONDS = 120
# When PerDay fires with no parseable hint, cool down for a full day.
DEFAULT_BLOCK_HOURS_WITHOUT_HINT = 24
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# Hard signal: Google quota id / metric name for per-day caps.
_PER_DAY_QUOTA = re.compile(r"PerDay", re.I)
# Retry delay hints often look like "retry in 3h42m12s" or "RetryInfo".
_RETRY_HINT = re.compile(
    r"(?:retry\s+(?:in|after)\s+([0-9hms\s.]+)|"
    r"retryDelay['\"]?\s*[:=]\s*['\"]?([0-9.]+s?)|"
    r"Please retry in ([^.'\"]+))",
    re.I,
)
# Parse "18h55m33s", "3h42m", "90s", "123.4s".
_DURATION_PARTS = re.compile(
    r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s?)?$",
    re.I,
)


class DailyQuotaError(Exception):
    """Raised when the model's daily quota is exhausted - do not retry."""

    def __init__(
        self,
        message: str,
        *,
        model_id: str | None = None,
        retry_hint: str | None = None,
        soft_budget: bool = False,
        blocked_until: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.model_id = model_id
        self.retry_hint = retry_hint
        self.soft_budget = soft_budget
        self.blocked_until = blocked_until


def extract_retry_hint(exc: BaseException | str) -> str | None:
    """Pull a human-readable retry delay hint from an error string, if any."""
    msg = str(exc)
    match = _RETRY_HINT.search(msg)
    if not match:
        return None
    for group in match.groups():
        if group:
            return group.strip()
    return None


def parse_retry_delay(hint: str | None) -> timedelta | None:
    """Parse a provider retry hint into a timedelta, or None if unparseable."""
    if not hint:
        return None
    text = hint.strip().lower().replace(" ", "")
    if not text:
        return None
    match = _DURATION_PARTS.fullmatch(text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = float(match.group(3) or 0)
    if hours == 0 and minutes == 0 and seconds == 0:
        return None
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def compute_blocked_until(
    hint: str | None,
    *,
    now: datetime | None = None,
    safety_margin_seconds: int = BLOCK_SAFETY_MARGIN_SECONDS,
) -> datetime:
    """UTC timestamp when it is safe to retry after a hard PerDay error."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delay = parse_retry_delay(hint)
    if delay is None:
        delay = timedelta(hours=DEFAULT_BLOCK_HOURS_WITHOUT_HINT)
    margin = timedelta(seconds=max(0, int(safety_margin_seconds)))
    return (now + delay + margin).astimezone(timezone.utc).replace(microsecond=0)


def format_reset_times(blocked_until: datetime) -> tuple[str, str]:
    """Return (utc_label, israel_label) for logging / UI."""
    if blocked_until.tzinfo is None:
        blocked_until = blocked_until.replace(tzinfo=timezone.utc)
    utc = blocked_until.astimezone(timezone.utc)
    israel = blocked_until.astimezone(ISRAEL_TZ)
    return (
        utc.strftime("%Y-%m-%d %H:%M:%S %Z"),
        israel.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )


def log_blocked_until(
    model_id: str | None,
    blocked_until: datetime,
    *,
    retry_hint: str | None = None,
) -> None:
    """Log observed reset time in UTC and Asia/Jerusalem."""
    utc_label, israel_label = format_reset_times(blocked_until)
    logger.warning(
        "Hard daily quota for model=%s; blocked_until UTC=%s Israel=%s "
        "(hint=%r, safety_margin_s=%s)",
        model_id,
        utc_label,
        israel_label,
        retry_hint,
        BLOCK_SAFETY_MARGIN_SECONDS,
    )


def is_daily_quota_error(exc: BaseException | str) -> bool:
    """True when the provider reports a per-day quota exhaustion."""
    msg = str(exc)
    if _PER_DAY_QUOTA.search(msg):
        return True
    # Common free-tier phrasing when the daily generate_content quota is gone.
    lower = msg.lower()
    if "quota exceeded" in lower and ("perday" in lower.replace("_", "").replace("-", "") or "daily" in lower):
        return True
    if "generate_requests_per_model_per_day" in lower:
        return True
    return False


def quota_day_key(
    *,
    tz_name: str = "UTC",
    now: datetime | None = None,
) -> str:
    """Calendar day string for llm_usage, using config timezone (default UTC)."""
    now = now or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - bad tz → UTC
        logger.warning("Invalid quota_day_timezone %r, using UTC", tz_name)
        tz = timezone.utc
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz).date().isoformat()


def resolve_pipeline_model(cfg: dict[str, Any], *, use_fallback: bool = False) -> str:
    """Active pipeline model id (primary or fallback_model when requested)."""
    primary = str(cfg.get("pipeline_model") or cfg.get("model_id") or "").strip()
    fallback = str(cfg.get("fallback_model") or "").strip()
    if use_fallback:
        if not fallback:
            raise ValueError(
                "--use-fallback-model requires fallback_model to be set in config/model.yaml"
            )
        return fallback
    return primary


def resolve_ask_model(cfg: dict[str, Any]) -> str:
    ask = str(cfg.get("ask_model") or "").strip()
    if ask:
        return ask
    return str(cfg.get("pipeline_model") or cfg.get("model_id") or "").strip()


def model_min_interval_seconds(cfg: dict[str, Any], model_id: str) -> float:
    """Per-model spacing between calls (5 rpm → 12s, 15 rpm → 4s)."""
    mapping = cfg.get("model_min_interval_seconds") or {}
    if isinstance(mapping, dict) and model_id in mapping:
        return max(0.0, float(mapping[model_id]))
    # Fall back to global llm_min_interval_seconds.
    return max(0.0, float(cfg.get("llm_min_interval_seconds", 0.5)))


def model_daily_limit(cfg: dict[str, Any], model_id: str) -> int | None:
    limits = cfg.get("model_daily_limits") or {}
    if not isinstance(limits, dict) or model_id not in limits:
        return None
    return int(limits[model_id])


def estimate_budgeted_calls(cfg: dict[str, Any]) -> dict[str, int]:
    """Rough max Gemini calls this process might make per model (soft check)."""
    batch = max(1, int(cfg.get("batch_size", 5)))
    max_items = max(0, int(cfg.get("max_items_per_run", 20)))
    rescore_limit = max(0, int(cfg.get("rescore_fallback_limit", 40)))
    pipeline_calls = math.ceil(max_items / batch) if max_items else 0
    rescore_calls = math.ceil(rescore_limit / batch) if rescore_limit else 0
    primary = str(cfg.get("pipeline_model") or cfg.get("model_id") or "")
    ask = str(cfg.get("ask_model") or "").strip()
    out: dict[str, int] = {}
    if primary:
        out[primary] = pipeline_calls + rescore_calls
    if ask and ask != primary:
        # Ask is interactive, budget 1 soft slot for the warning check.
        out[ask] = out.get(ask, 0) + 1
    return out


def warn_if_budgets_exceed_limits(cfg: dict[str, Any]) -> list[str]:
    """Log/return warnings when estimated calls exceed configured daily limits."""
    warnings: list[str] = []
    estimated = estimate_budgeted_calls(cfg)
    for model_id, needed in estimated.items():
        limit = model_daily_limit(cfg, model_id)
        if limit is None:
            continue
        if needed > limit:
            msg = (
                f"Budgeted calls for {model_id} (~{needed}) exceed "
                f"model_daily_limits[{model_id}]={limit}"
            )
            warnings.append(msg)
            logger.warning(msg)
    return warnings


def raise_if_daily_quota(exc: BaseException, *, model_id: str | None = None) -> None:
    """If ``exc`` is a daily-quota error, raise DailyQuotaError (with hint logged)."""
    if not is_daily_quota_error(exc):
        return
    hint = extract_retry_hint(exc)
    blocked_until = compute_blocked_until(hint)
    log_blocked_until(model_id, blocked_until, retry_hint=hint)
    raise DailyQuotaError(
        f"Daily quota exhausted for model {model_id}: {exc}",
        model_id=model_id,
        retry_hint=hint,
        soft_budget=False,
        blocked_until=blocked_until,
    ) from exc


def friendly_quota_message(
    *,
    blocked_until: datetime | None = None,
    model_id: str | None = None,
) -> str:
    """User-facing Ask / UI message when a model is hard-blocked or hit PerDay."""
    base = (
        "Ask the Digest has hit today's Gemini free-tier quota"
        + (f" for {model_id}" if model_id else " for this model")
        + "."
    )
    if blocked_until is not None:
        utc_label, israel_label = format_reset_times(blocked_until)
        return (
            f"{base} Please try again after approximately {israel_label} "
            f"({utc_label}). Or ask your operator to point ask_model at another "
            "model id in config/model.yaml."
        )
    return (
        f"{base} Please try again after the quota resets, or ask your operator "
        "to point ask_model at another model id in config/model.yaml."
    )
