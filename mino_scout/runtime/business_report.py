"""Compact business projection. Raw events remain diagnostic evidence."""
from __future__ import annotations

_AUTOMATION = {"locator_missing", "control_unknown", "locator_not_found", "locator_ambiguous", "method_unknown",
               "script_failed", "check_empty", "assertion_missing", "model_policy_denied", "model_policy_invalid",
               "model_budget_exceeded", "ocr_low_confidence", "ocr_config_invalid"}


def _short(value, limit=800):
    import json
    if value is None:
        return "未提供"
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True))[:limit]


def _describe(row):
    """Describe recorded checks without another model request or hiding failed checks."""
    expected, actual = row.get("expected"), row.get("actual")
    checker = row.get("checker")
    if isinstance(expected, dict) and isinstance(actual, dict):
        control, wanted = expected.get("control", ""), expected.get("expected")
        if checker in {"text", "ocr_text"}:
            text = expected.get("text", "")
            condition = f"{'显示' if wanted else '不显示'}「{text}」" if text else f"控件 {control} 出现"
            if actual.get("matched"):
                observed = "已找到「" + "、".join(actual["matched"]) + "」"
            elif "matched" in actual:
                observed = "未找到目标文字；当前文字：" + "、".join(actual.get("observed", []))
            else:
                observed = f"匹配到 {actual.get('matches', 0)} 个控件"
        elif checker == "state":
            state = expected.get("state")
            labels = {"visible": ("不可见", "可见"), "enabled": ("不可用", "可用"),
                      "checked": ("未勾选", "已勾选"), "selected": ("未选中", "已选中"), "focused": ("未聚焦", "已聚焦")}
            names = labels.get(state, ("否", "是"))
            condition = f"控件 {control} 应为{names[bool(wanted)]}"
            value = actual.get(state)
            observed = names[value] if type(value) is bool else "未取得明确的控件状态"
        elif checker == "count":
            condition, observed = f"控件 {control} 数量应为 {wanted}", f"实际匹配 {actual.get('count', '未知')} 项"
        elif checker == "value":
            condition = f"控件 {control} 的值应为「{_short(wanted)}」"
            value = actual.get("value", actual.get("text"))
            observed = "实际值：「" + _short(value) + "」" if value is not None else "未取得可公开读取的值"
        else:
            return _short(expected), _short(actual)
        if row.get("status") == "unknown":
            observed = "未核验：" + str(row.get("message") or "当前观察不足以判定") + "；" + observed
        return condition, observed
    if checker == "on_page" and isinstance(actual, dict):
        return f"页面应为 {expected}", "页面锚点已匹配" if actual.get("matched") else "页面锚点未匹配"
    return _short(expected), _short(actual if actual is not None else row.get("message"))


def attribution(status, reason):
    if reason in {"data_missing", "fact_unavailable", "prerequisite_unavailable", "resource_account_missing", "resource_busy", "otp_requires_human", "otp_environment_unavailable", "nexus_rejected", "nexus_unreachable", "tested_build_mismatch", "tested_build_unverified"}:
        return "blocked"
    if reason in _AUTOMATION:
        return "automation"
    if reason.startswith("source_"):
        return "source_mismatch"
    if reason in {"assertion_failed", "vlm_failed", "page_mismatch"}:
        return "suspected_product"
    return "blocked" if status in {"blocked", "declined", "skipped", "cancelled"} else "unknown"


def build_report(meta, outcome, events):
    snapshot = meta.get("source_snapshot") or {}
    expectations = snapshot.get("expectations") or []
    assertions = [(seq, row) for seq, kind, row in events if kind == "assertion/result"]
    entries = []
    for clause in expectations:
        ref = str(clause.get("id") or "")
        observed = [(seq, row) for seq, row in assertions if row.get("source_ref") == ref]
        # A later passing page assertion must never hide an earlier business failure.
        failed = [(seq, row) for seq, row in observed if row.get("status") == "fail"]
        unknown = [(seq, row) for seq, row in observed if row.get("status") not in {"pass", "fail", "accepted"}]
        chosen = (failed or unknown or observed)
        seq, row = chosen[0] if failed or unknown else chosen[-1] if chosen else (0, {})
        status = "pass" if row.get("status") == "accepted" else row.get("status", "not_run") if not observed or row.get("status") in {"pass", "fail", "unknown", "accepted"} else "unknown"
        reason = str(row.get("reason_code") or ("assertion_failed" if status == "fail" else ""))
        descriptions = [_describe(value) for _, value in failed + unknown + [pair for pair in observed if pair[1].get("status") == "pass"]]
        expected_text = "\n".join(value[0] for value in descriptions) if descriptions else _short(clause.get("text"))
        actual_text = "\n".join(value[1] for value in descriptions) if descriptions else "尚未执行到该预期"
        stopped = [(s, value) for s, kind, value in events if kind in {"node/leave", "model/result"}
                   and value.get("source_ref") == ref and (value.get("failed") or value.get("status") in {"blocked", "fail", "unknown"})]
        if not observed and stopped:
            seq, row = stopped[0]
            status, reason = "unknown", str(row.get("reason_code") or outcome.get("reason_code") or "assertion_missing")
            actual_text = "未完成有效断言：" + _short(outcome.get("summary"))
        if row.get("status") == "fail" and row.get("actual") is None and ref == outcome.get("failure_source_ref"):
            actual_text = _short(outcome.get("summary"))
        entries.append({"source_ref": ref, "title": str(clause.get("text") or "")[:300], "status": status,
                        "expected": expected_text[:800], "actual": actual_text[:800],
                        "checker": str(row.get("checker") or ""), "event_seq": seq,
                        "artifact_id": str(row.get("artifact_id") or ""), "reason_code": reason,
                        "attribution": attribution(status, reason) if status != "pass" and status != "not_run" else ""})
    counts = {"total": len(entries), "pass": 0, "fail": 0, "unknown": 0, "not_run": 0}
    for entry in entries:
        counts[entry["status"] if entry["status"] in counts and entry["status"] != "total" else "unknown"] += 1
    model_calls = [(seq, row) for seq, kind, row in events if kind == "model/call"]
    model_results = [row for seq, kind, row in events if kind == "model/result" and row.get("request_id")]
    reported = [row["usage"]["total_tokens"] for row in model_results if isinstance(row.get("usage"), dict)
                and type(row["usage"].get("total_tokens")) is int]
    reason = str(outcome.get("reason_code") or "")
    failure_ref = str(outcome.get("failure_source_ref") or "")
    failures = [(seq, row) for seq, kind, row in events if kind in {"tool/result", "assertion/result", "node/leave", "model/result"}
                and row.get("source_ref") == failure_ref and (row.get("status") in {"fail", "blocked", "unknown"} or row.get("failed"))]
    failure = {"source_ref": failure_ref, "title": str(outcome.get("failure_title") or "")[:300],
               "actual": _short(outcome.get("summary")), "event_seq": failures[0][0] if failures else 0}
    auxiliary = [row for _, row in assertions if row.get("source_ref") not in {str(c.get("id") or "") for c in expectations}]
    auxiliary_counts = {"total": len(auxiliary), "pass": sum(r.get("status") in {"pass", "accepted"} for r in auxiliary),
                        "fail": sum(r.get("status") == "fail" for r in auxiliary),
                        "unknown": sum(r.get("status") not in {"pass", "fail", "accepted"} for r in auxiliary)}
    purpose = "preparation" if (meta.get("source_context") or {}).get("purpose") == "prepare" else meta.get("run_purpose", "unclassified")
    return {"version": 1, "run_purpose": purpose, "counts": counts,
            "auxiliary_assertions": auxiliary_counts,
            "context": {key: str(meta.get(key) or "") for key in ("task_id", "session_id", "attempt_id", "case_id", "node_id", "revision", "script_path", "source_hash")},
            "items": entries[:50], "items_truncated": len(entries) > 50,
            "attribution": attribution(outcome.get("status", ""), reason) if outcome.get("status") != "pass" else "",
            "reason_code": reason, "model_usage": {"calls": len(model_calls),
                "llm_calls": sum(row.get("kind") == "llm" for _, row in model_calls),
                "vlm_calls": sum(row.get("kind") == "vlm" for _, row in model_calls),
                "elapsed_ms": sum(row.get("elapsed_ms", 0) for row in model_results),
                "total_tokens": sum(reported) if reported else 0 if not model_calls else None,
                "usage_complete": len(model_results) == len(model_calls) and len(reported) == len(model_calls)},
            **({"failure": failure} if outcome.get("status") != "pass" else {})}
