"""Process-wide spacing between Gemini calls (works with concurrent workers)."""

from __future__ import annotations

import threading
import time


class CallIntervalGate:
    """Ensure at least ``min_interval`` seconds between successive acquire() calls."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, *, sleep_fn=time.sleep, monotonic_fn=time.monotonic) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = monotonic_fn()
            delay = self._next_allowed - now
            if delay > 0:
                sleep_fn(delay)
                now = monotonic_fn()
            self._next_allowed = now + self.min_interval


# Shared gate for the process — reset via configure_llm_interval() at run start.
_GATE = CallIntervalGate(0.0)


def configure_llm_interval(min_interval_seconds: float) -> CallIntervalGate:
    global _GATE
    _GATE = CallIntervalGate(min_interval_seconds)
    return _GATE


def wait_llm_interval() -> None:
    _GATE.wait()
