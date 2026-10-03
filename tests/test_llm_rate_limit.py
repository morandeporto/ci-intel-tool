"""Tests for cross-worker Gemini call spacing."""

from __future__ import annotations

from src.process.llm_rate_limit import CallIntervalGate, configure_llm_interval


def test_call_interval_gate_spaces_calls() -> None:
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def mono() -> float:
        return clock["t"]

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock["t"] += delay

    gate = CallIntervalGate(0.5)
    gate.wait(sleep_fn=sleep, monotonic_fn=mono)
    assert sleeps == []
    clock["t"] = 0.2
    gate.wait(sleep_fn=sleep, monotonic_fn=mono)
    assert sleeps and abs(sleeps[0] - 0.3) < 1e-9


def test_configure_llm_interval_replaces_gate() -> None:
    gate = configure_llm_interval(1.25)
    assert gate.min_interval == 1.25
