"""An explicit generation operation. The Agent/case decides when to call it.

Copy this example into the application's script repository flows/ directory.
Application-specific phases and output selectors come from current Scout evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from mino_scout.runtime.business_state import ObjectField, Phase, WaitResult, read_object, validate_wait, wait_phase


@dataclass(frozen=True)
class GenerationResult:
    wait: WaitResult
    object: dict


def generate_object(app, scout, *, name: str, name_control: str, submit_control: str,
                    phases: Sequence[Phase], output_fields: Mapping[str, ObjectField],
                    target: str, failures: Sequence[str] = (), timeout_ms: int = 60000) -> GenerationResult:
    """Submit exactly once, wait for declared business phases, and preserve observed output.

    The calling case binds confirmed fields with case.data.bind and decides which original
    expectation to check. A failed wait does not trigger another submission or a new account.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Generation requires an explicit name for this run")
    if not name_control or not submit_control or not output_fields or len(output_fields) > 20:
        raise ValueError("Generation requires explicit controls and 1..20 output fields")
    if any(not spec.control or spec.field not in {"text", "value"} for spec in output_fields.values()):
        raise ValueError("Output fields require controls and exact text/value reads")
    validate_wait(phases, target=target, failures=failures, timeout_ms=timeout_ms)
    values = {"request": {"name": name}}
    app.input(name_control, name)
    app.tap(submit_control)
    result = wait_phase(app, scout, phases, target=target, failures=failures,
                        values=values, timeout_ms=timeout_ms)
    observed = read_object(app, output_fields, values=values)
    return GenerationResult(result, observed)
