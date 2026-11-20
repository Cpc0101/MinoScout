from __future__ import annotations

import pytest

from mino_scout.platform.web.web_v5 import (
    DEFAULT_WEB_LANES,
    MAX_WEB_LANES,
    WebV5Runtime,
    configured_lanes,
)


def test_configured_lanes_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("MINO_WEB_PLAYWRIGHT_LANES", raising=False)
    assert configured_lanes() == DEFAULT_WEB_LANES == 8

    monkeypatch.setenv("MINO_WEB_PLAYWRIGHT_LANES", "0")
    assert configured_lanes() == 1

    monkeypatch.setenv("MINO_WEB_PLAYWRIGHT_LANES", "999")
    assert configured_lanes() == MAX_WEB_LANES == 64

    monkeypatch.setenv("MINO_WEB_PLAYWRIGHT_LANES", "invalid")
    assert configured_lanes() == DEFAULT_WEB_LANES


def test_run_debug_and_script_share_one_finite_pool():
    runtime = WebV5Runtime(lanes=8)
    kinds = ["run", "debug", "script", "v5", "run", "debug", "script", "v5"]

    for index, kind in enumerate(kinds):
        assert runtime.claim_external_state(f"claim-{index}", run_id=f"run-{index}", kind=kind) == "created"

    assert runtime.capacity() == {
        "lanes_total": 8,
        "lanes_free": 0,
        "lanes_quarantined": 0,
    }
    assert runtime.claim_external_state("claim-8", run_id="run-8", kind="debug") == "busy"
    assert runtime.claim_external_state("claim-0", run_id="run-0", kind="run") == "reused"
    assert runtime.claim_external_state("claim-0", run_id="run-0", kind="debug") == "busy"

    assert runtime.release_external("claim-0") is True
    assert runtime.claim_external_state("claim-8", run_id="run-8", kind="debug") == "created"
    assert runtime.capacity()["lanes_free"] == 0


def test_quarantined_lane_consumes_shared_capacity():
    runtime = WebV5Runtime(lanes=2)
    assert runtime.claim_external_state("debug-1", run_id="debug-1", kind="debug") == "created"
    assert runtime.claim_external_state("run-2", run_id="run-2", kind="run") == "created"
    assert runtime.quarantine_external("debug-1") is True

    snapshot = runtime.capacity()
    assert snapshot == {
        "lanes_total": 2,
        "lanes_free": 0,
        "lanes_quarantined": 1,
    }
    assert runtime.claim_external_state("script-3", run_id="script-3", kind="script") == "busy"
    assert runtime.release_external("debug-1") is False
    assert runtime.claim_external_state("debug-1", run_id="debug-1", kind="debug") == "quarantined"


def test_active_claims_report_execution_labels_without_a_second_pool():
    runtime = WebV5Runtime(lanes=3)
    runtime.claim_external_state("run-1", run_id="run-1", kind="run")
    runtime.claim_external_state("debug-2", run_id="debug-2", kind="debug")
    runtime.claim_external_state("script-3", run_id="script-3", kind="script")

    assert runtime.active_claims() == [
        {"run_id": "run-1", "kind": "run"},
        {"run_id": "debug-2", "kind": "debug"},
        {"run_id": "script-3", "kind": "script"},
    ]
    with pytest.raises(ValueError, match="web_lane_count_invalid"):
        WebV5Runtime(lanes=65)
