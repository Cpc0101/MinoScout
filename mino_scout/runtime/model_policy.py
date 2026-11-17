"""Literal case policy; every generated-model request passes this gate."""
from __future__ import annotations

import ast
import time
import uuid


def declarations(tree):
    values = {}
    for node in tree.body:
        names = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in names:
            if isinstance(target, ast.Name) and target.id in {"MODEL_POLICY", "RUN_PURPOSE"}:
                if target.id in values:
                    raise ValueError(f"Duplicate {target.id}")
                try:
                    values[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    raise ValueError(f"{target.id} must be a literal") from None
    purpose = values.get("RUN_PURPOSE", "regression")
    if purpose not in {"authoring", "verification", "regression"}:
        raise ValueError("RUN_PURPOSE must be authoring, verification or regression")
    policy = values.get("MODEL_POLICY", {"mode": "off"})
    if not isinstance(policy, dict) or policy.get("mode") not in {"off", "allow"}:
        raise ValueError("MODEL_POLICY requires mode=off or allow")
    if set(policy) - {"mode", "max_calls", "timeout_ms", "total_timeout_ms", "allow"}:
        raise ValueError("Unknown MODEL_POLICY field")
    if policy["mode"] == "off":
        if policy.get("allow"):
            raise ValueError("mode=off cannot allow model requests")
        return purpose, policy
    for field, ceiling in (("max_calls", 100), ("timeout_ms", 60000), ("total_timeout_ms", 300000)):
        value = policy.get(field)
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"MODEL_POLICY {field} must be 1..{ceiling}")
    rules = policy.get("allow")
    if not isinstance(rules, list) or not rules:
        raise ValueError("MODEL_POLICY allow must contain explicit rules")
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"kind", "source_ref", "purpose", "reason", "max_calls"}:
            raise ValueError("Each model rule requires kind, source_ref, purpose, reason, max_calls")
        if rule["kind"] not in {"llm", "vlm"} or rule["purpose"] not in {"text_assertion", "visual_assertion", "exploration"}:
            raise ValueError("Invalid model kind or purpose")
        if rule["purpose"] == "exploration" and purpose != "authoring":
            raise ValueError("Model exploration is only allowed for RUN_PURPOSE=authoring")
        if rule["purpose"] == "text_assertion" and rule["kind"] != "llm" or rule["purpose"] == "visual_assertion" and rule["kind"] != "vlm":
            raise ValueError("Model kind does not match its purpose")
        if not all(isinstance(rule[k], str) and rule[k].strip() for k in ("source_ref", "reason")):
            raise ValueError("Model exceptions need an original clause and a reason")
        if type(rule["max_calls"]) is not int or not 1 <= rule["max_calls"] <= policy["max_calls"]:
            raise ValueError("Rule max_calls exceeds case budget")
        key = (rule["kind"], rule["source_ref"], rule["purpose"])
        if key in seen:
            raise ValueError("Duplicate model exception")
        seen.add(key)
    return purpose, policy


class ModelGate:
    def __init__(self, case, path):
        self.case = case
        self.run_purpose, self.policy = declarations(ast.parse(path.read_text(encoding="utf-8")))
        self.calls, self.elapsed_ms = 0, 0
        self.by_rule = {}

    def begin(self, kind, purpose):
        from mino_scout.runtime.script_exec import ScriptAbort
        case = self.case
        case._guard()
        if case._latched_error:
            raise case._latched_error
        rule = next((r for r in self.policy.get("allow", []) if
                     (r["kind"], r["source_ref"], r["purpose"]) == (kind, case._source_ref, purpose)), None)
        key = (kind, case._source_ref, purpose)
        code = "model_policy_denied"
        if self.policy["mode"] == "allow" and rule and case._source_ref in case._source_clauses:
            if self.calls < self.policy["max_calls"] and self.by_rule.get(key, 0) < rule["max_calls"]:
                remain = self.policy["total_timeout_ms"] - self.elapsed_ms
                if case.deadline:
                    remain = min(remain, (case.deadline - time.monotonic()) * 1000)
                if remain > 0:
                    self.calls += 1
                    self.by_rule[key] = self.by_rule.get(key, 0) + 1
                    request = {"request_id": "model_" + uuid.uuid4().hex, "kind": kind, "purpose": purpose,
                               "source_ref": case._source_ref, "step_id": case._step_id,
                               "run_purpose": self.run_purpose, "reason": rule["reason"],
                               "timeout_ms": min(self.policy["timeout_ms"], remain)}
                    self._record("model/call", request)
                    return request
            code = "model_budget_exceeded"
        self._record("model/result", {"kind": kind, "purpose": purpose, "source_ref": case._source_ref,
                                      "step_id": case._step_id, "status": "blocked", "reason_code": code})
        error = ScriptAbort("模型调用未声明或超出预算，请使用确定性断言或补齐原文关联的模型例外", reason_code=code, status="blocked")
        case._latched_error = case._latched_error or error
        raise error

    def finish(self, request, started, *, status, reason_code="", usage=None):
        elapsed = int((time.monotonic() - started) * 1000)
        self.elapsed_ms += elapsed
        self._record("model/result", {**request, "status": status, "reason_code": reason_code,
                                      "elapsed_ms": elapsed, "usage": usage, "usage_reported": usage is not None})
        self.case._guard()

    def _record(self, kind, data):
        if self.case._recorder:
            self.case._recorder.append(kind, {**data, "source_hash": self.case._source_hash})


def validate_model_usage(root, trees):
    """Follow repository imports and hooks; runtime remains authoritative for dynamic calls."""
    findings = []
    for path, tree in trees.items():
        if "tests" not in path.relative_to(root).parts or not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
            continue
        try:
            _, policy = declarations(tree)
        except ValueError as exc:
            findings.append((str(path.relative_to(root)), 1, "model_policy", str(exc), "error"))
            continue
        pending = [path, root / "pretest.py", root / "posttest.py"]
        visited = set()
        while pending:
            current = pending.pop()
            if current in visited or current not in trees:
                continue
            visited.add(current)
            for node in ast.walk(trees[current]):
                modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for module in modules:
                    base = current.parent if isinstance(node, ast.ImportFrom) and node.level else root
                    if isinstance(node, ast.ImportFrom):
                        for _ in range(max(0, node.level - 1)):
                            base = base.parent
                    candidate = base.joinpath(*module.split("."))
                    pending.extend([candidate.with_suffix(".py"), candidate / "__init__.py"])
                    if isinstance(node, ast.ImportFrom):
                        pending.extend(candidate / (name.name + ".py") for name in node.names)
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                name = node.func.attr
                keywords = {k.arg: k.value for k in node.keywords}
                prompt = keywords.get("prompt") or (node.args[1] if name == "expect" and len(node.args) > 1 else None)
                if name == "expect" and prompt is not None:
                    allowed = keywords.get("allow_model")
                    if not isinstance(allowed, ast.Constant) or allowed.value is not True:
                        findings.append((str(current.relative_to(root)), node.lineno, "model_implicit", "expect(prompt=...) requires allow_model=True and an explicit case policy; prefer expect_text/state/count/value", "error"))
                        continue
                elif name not in {"llm", "vlm", "vlm_frame"}:
                    continue
                kind = "llm" if name == "llm" else "vlm" if name != "expect" else None
                if policy["mode"] != "allow" or kind and not any(r["kind"] == kind for r in policy.get("allow", [])):
                    findings.append((str(current.relative_to(root)), node.lineno, "model_policy", f"Model call is not allowed by {path.relative_to(root)} MODEL_POLICY", "error"))
    return list(dict.fromkeys(findings))
