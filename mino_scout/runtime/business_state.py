"""Reusable Scout observations for application flows. All UI reads use the supplied app.

Application flows supply real page controls and explicit phase rules. This module never
chooses data, creates it on a miss, retries a write, leases accounts, or orchestrates cases.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Signal:
    control: str
    field: str = "visible"
    expected: Any = True
    text: str = ""


@dataclass(frozen=True)
class Phase:
    name: str
    signals: tuple[Signal, ...]


@dataclass(frozen=True)
class PhaseObservation:
    phase: str | None
    reason: str
    matches: tuple[str, ...]
    observations: tuple[dict, ...]


@dataclass(frozen=True)
class WaitResult:
    reached: bool
    reason: str
    observation: PhaseObservation
    polls: int
    elapsed_ms: int


def _validate(phases: Sequence[Phase]):
    if not phases or len(phases) > 20 or len({p.name for p in phases}) != len(phases):
        raise ValueError("Supply 1..20 phases with distinct names")
    for phase in phases:
        if not phase.name or not phase.signals or len(phase.signals) > 20:
            raise ValueError("Each phase needs a name and 1..20 explicit business signals")
        for signal in phase.signals:
            if signal.field not in {"visible", "enabled", "checked", "selected", "focused", "value", "text", "count", "text_present"}:
                raise ValueError("Unsupported signal field")
            if not signal.control and signal.field != "text_present" or signal.field == "text_present" and not signal.text:
                raise ValueError("A signal needs a control, or explicit text_present text")
            if signal.field in {"visible", "enabled", "checked", "selected", "focused", "text_present"} and type(signal.expected) is not bool:
                raise ValueError("State signals require boolean expectations")
            if signal.field == "count" and (type(signal.expected) is not int or signal.expected < 0):
                raise ValueError("Count signals require a nonnegative integer")
            if signal.field == "text" and not isinstance(signal.expected, str):
                raise ValueError("Text signals require exact text")


def _control(app, control: str, values: Mapping[str, Any]):
    return app.bind(control, **values) if control and values else control


def _bind_phases(app, phases, values):
    controls = {}
    result = []
    for phase in phases:
        signals = []
        for signal in phase.signals:
            if signal.control not in controls:
                controls[signal.control] = _control(app, signal.control, values)
            signals.append(Signal(controls[signal.control], signal.field, signal.expected, signal.text))
        result.append(Phase(phase.name, tuple(signals)))
    return tuple(result)


def observe_phase(app, phases: Sequence[Phase], *, values: Mapping[str, Any] | None = None) -> PhaseObservation:
    """All signals must be known and agree. Multiple matching phases stay unresolved."""
    _validate(phases)
    values = values or {}
    observations, matches, cache = [], [], {}
    unresolved = False
    for phase in phases:
        matched, uncertain = True, False
        for signal in phase.signals:
            key = (signal.control, signal.field, signal.text)
            if key not in cache:
                cache[key] = app.observe(_control(app, signal.control, values), field=signal.field, text=signal.text).data
            fact = cache[key]
            known = fact.get("known") is True
            equal = known and type(fact.get("value")) is type(signal.expected) and fact["value"] == signal.expected
            if known and not equal:
                matched = False
            uncertain = uncertain or not known
            observations.append({"phase": phase.name, "control": signal.control, "field": signal.field,
                                 "expected": signal.expected, **fact})
        if matched and not uncertain:
            matches.append(phase.name)
        unresolved = unresolved or matched and uncertain
    # A competing rule with insufficient evidence can still be true; do not pick a winner.
    if len(matches) == 1 and not unresolved:
        return PhaseObservation(matches[0], "matched", tuple(matches), tuple(observations))
    reason = "phase_conflict" if len(matches) > 1 else "observation_insufficient" if unresolved else "phase_unknown"
    return PhaseObservation(None, reason, tuple(matches), tuple(observations))


def validate_wait(phases: Sequence[Phase], *, target: str, failures: Sequence[str] = (),
                  timeout_ms: int = 30000, interval_ms: int = 300, stable_polls: int = 1):
    """Validate a declared wait before any explicit submission can change the application."""
    _validate(phases)
    names = {p.name for p in phases}
    if target not in names or set(failures) - names or target in failures:
        raise ValueError("Target and failure phases must be distinct declared phases")
    if type(timeout_ms) is not int or not 0 <= timeout_ms <= 600000:
        raise ValueError("timeout_ms must be 0..600000")
    if type(interval_ms) is not int or not 1 <= interval_ms <= 10000:
        raise ValueError("interval_ms must be 1..10000")
    if type(stable_polls) is not int or not 1 <= stable_polls <= 20:
        raise ValueError("stable_polls must be 1..20")


def wait_phase(app, scout, phases: Sequence[Phase], *, target: str, failures: Sequence[str] = (),
               values: Mapping[str, Any] | None = None, timeout_ms: int = 30000, interval_ms: int = 300,
               stable_polls: int = 1) -> WaitResult:
    """Poll observations only. Return facts on failure/timeout for the case to judge."""
    validate_wait(phases, target=target, failures=failures, timeout_ms=timeout_ms,
                  interval_ms=interval_ms, stable_polls=stable_polls)
    # The bound selector contains ordinary values, and each observation resolves it afresh.
    # Bind once per wait rather than accumulating temporary control keys on every poll.
    phases = _bind_phases(app, phases, values) if values else phases
    started, polls, stable = time.monotonic(), 0, 0
    deadline = started + timeout_ms / 1000
    while True:
        observation = observe_phase(app, phases)
        polls += 1
        stable = stable + 1 if observation.phase == target else 0
        elapsed = int((time.monotonic() - started) * 1000)
        if observation.phase in failures:
            return WaitResult(False, "business_failed", observation, polls, elapsed)
        if stable >= stable_polls:
            return WaitResult(True, "reached", observation, polls, elapsed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WaitResult(False, "timeout", observation, polls, elapsed)
        scout.wait(min(interval_ms, max(1, int(remaining * 1000))))


@dataclass(frozen=True)
class ObjectField:
    control: str
    field: str = "value"


def read_object(app, fields: Mapping[str, ObjectField], *, values: Mapping[str, Any] | None = None) -> dict:
    """Capture declared business values and their evidence; no fabricated IDs or defaults."""
    if not fields or len(fields) > 20:
        raise ValueError("Supply 1..20 object fields")
    data, missing, evidence = {}, [], {}
    for name, spec in fields.items():
        if spec.field not in {"text", "value"}:
            raise ValueError("Object fields must read exact text or value")
        fact = app.observe(_control(app, spec.control, values or {}), field=spec.field).data
        evidence[name] = fact
        if fact.get("known") is True and fact.get("value") not in (None, ""):
            data[name] = fact["value"]
        else:
            missing.append(name)
    return {"data": data, "missing": missing, "known": not missing, "evidence": evidence}


def expect_object(app, expected: Mapping[str, Any], fields: Mapping[str, ObjectField], *,
                  values: Mapping[str, Any] | None = None, timeout_ms: int = 0):
    """Verify the current displayed object inside the calling case's check scope.

    The case chooses the expected mapping, including cross-application ID mappings.
    Use a selector for the actual destination preview, not an alternate matching card.
    """
    if not expected or set(expected) != set(fields):
        raise ValueError("Expected values must cover exactly the declared identity fields")
    for name, spec in fields.items():
        if spec.field not in {"text", "value"} or expected[name] in (None, ""):
            raise ValueError("Identity checks need known text/value fields")
        control = _control(app, spec.control, values or {})
        if spec.field == "text":
            app.expect_text(expected[name], control=control, timeout_ms=timeout_ms)
        else:
            app.expect_value(control, expected[name], timeout_ms=timeout_ms)
