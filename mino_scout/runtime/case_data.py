"""Explicit runtime values and missing-input evidence. No recovery policy."""
from __future__ import annotations
from copy import deepcopy
from mino_scout.server_comm.protocol import validate_runtime_data


class CaseData:
    def __init__(self, case, inputs=None):
        self._case = case
        self._inputs = validate_runtime_data({} if inputs is None else inputs)
        self._outputs = {}
        self._missing = {}

    @property
    def inputs(self):
        return deepcopy(self._inputs)

    def get(self, name, default=None):
        self._case._guard()
        return deepcopy(self._outputs.get(name, self._inputs.get(name, default)))

    def bind(self, name, value):
        self._case._guard()
        from mino_scout.runtime.script_exec import ScriptAbort
        from mino_scout.runtime.records import scrub
        try:
            values = validate_runtime_data({**self._outputs, name: value})
        except ValueError as exc:
            raise ScriptAbort(str(exc), reason_code="data_binding_invalid", status="blocked") from None
        if scrub(values) != values:
            raise ScriptAbort("业务对象不能包含凭据", reason_code="data_binding_invalid", status="blocked")
        self._outputs = values
        self._missing.pop(name, None)
        self._record("data/bind", {"name": name, "value": values[name]})
        return deepcopy(values[name])

    def require(self, name, *, description="", kind="", attributes=None):
        self._case._guard()
        value = self.get(name)
        if value is not None:
            return value
        from mino_scout.runtime.script_exec import ScriptAbort
        try:
            from mino_scout.server_comm.protocol import validate_data_summary
            need = {"name": name, "description": description, "kind": kind, "attributes": {} if attributes is None else attributes}
            validate_data_summary({"data_missing": [*self._missing.values(), need]})
        except ValueError as exc:
            raise ScriptAbort(str(exc), reason_code="data_binding_invalid", status="blocked") from None
        if len(self._missing) >= 20 and name not in self._missing:
            raise ScriptAbort("本次数据缺口过多", reason_code="data_binding_invalid", status="blocked")
        self._missing[name] = need
        self._record("data/missing", need)
        error = ScriptAbort(description or f"缺少输入数据：{name}", reason_code="data_missing", status="blocked")
        self._case._latched_error = self._case._latched_error or error
        raise error

    def snapshot(self):
        return {"data_outputs": deepcopy(self._outputs), "data_missing": list(deepcopy(self._missing).values())}

    def _record(self, event_type, data):
        case = self._case
        event = {**data, "source_ref": case._source_ref, "source_hash": case._source_hash, "step_id": case._step_id}
        if case._recorder:
            case._recorder.append(event_type, event)
        else:
            from mino_scout.runtime.script_exec import ActionResult
            case._report("data_binding", ActionResult(ok=True, status="pass", summary=event_type, data=event), "script", 0)
