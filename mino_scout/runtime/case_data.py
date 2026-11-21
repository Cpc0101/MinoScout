"""Explicit runtime values and missing-input evidence. No recovery policy."""
from __future__ import annotations
import hashlib
import random
import re
from copy import deepcopy
from datetime import date, timedelta

from mino_scout.server_comm.protocol import validate_runtime_data


class CaseData:
    def __init__(self, case, inputs=None):
        self._case = case
        self._inputs = validate_runtime_data({} if inputs is None else inputs)
        self._outputs = {}
        self._missing = {}
        self._generation_counts: dict[str, int] = {}
        self._unique_namespace = ""

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

    def generate(self, kind, *, seed=0, version="v1", locale="zh-CN",
                 namespace="", unique=False, **options):
        """Generate a replayable synthetic input without creating business data.

        The same kind/version/seed/locale/namespace/unique call sequence produces
        the same value for the same generator version and call order; values are
        not promised to match across Python runtime versions. Unique mode binds
        the first explicit namespace to this CaseData instance. Later unique calls
        may omit the namespace, while a different namespace is blocked. Callers
        must inject a run-unique namespace on the first unique call.
        Evidence records metadata, length and SHA-256, never the generated value.
        """
        self._case._guard()
        from mino_scout.runtime.script_exec import ScriptAbort

        def invalid(code, message):
            raise ScriptAbort(message, reason_code=code, status="blocked")

        allowed = {"name", "email", "text", "date", "number"}
        name = str(kind or "").strip().lower()
        if name not in allowed:
            invalid("data_generator_invalid", "生成类型必须是 name/email/text/date/number")
        if type(seed) is not int:
            invalid("data_generator_invalid", "seed 必须是整数")
        version = str(version or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", version):
            invalid("data_generator_invalid", "version 格式无效")
        locale = str(locale or "").strip()
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*", locale):
            invalid("data_generator_invalid", "locale 格式无效")
        namespace = str(namespace or "").strip()
        if unique:
            if self._unique_namespace:
                if namespace and namespace != self._unique_namespace:
                    invalid(
                        "data_generator_namespace_changed",
                        "同一 CaseData 的 unique namespace 不能变更",
                    )
                namespace = self._unique_namespace
            elif not namespace:
                invalid("data_generator_namespace_required", "首个 unique=True 调用必须显式提供 namespace")
            else:
                self._unique_namespace = namespace
        if len(namespace) > 160 or any(ord(ch) < 32 for ch in namespace):
            invalid("data_generator_invalid", "namespace 无效")
        if unique:
            counter_key = "|".join((name, version, locale, namespace, str(seed)))
            ordinal = self._generation_counts.get(counter_key, 0) + 1
            self._generation_counts[counter_key] = ordinal
        else:
            ordinal = 0
        material = "\x1f".join((version, locale, namespace, name, str(seed), str(ordinal)))
        digest = hashlib.sha256(material.encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest, "big"))
        value = self._generated_value(name, rng, locale, options, invalid)
        encoded = value if isinstance(value, str) else str(value)
        evidence = {
            "type": name, "version": version, "seed": seed, "locale": locale,
            "unique": bool(unique), "namespace": namespace,
            **({"ordinal": ordinal} if unique else {}),
            "value_length": len(encoded),
            "value_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        }
        self._record("data/generate", evidence)
        return deepcopy(value)

    @staticmethod
    def _generated_value(name, rng, locale, options, invalid):
        if name == "name":
            if locale.lower().startswith("zh"):
                given, family = ("子涵", "雨桐", "嘉宁", "思远", "若溪", "景行"), ("林", "陈", "周", "许", "沈", "顾")
            else:
                given, family = ("Alex", "Taylor", "Morgan", "Jordan", "Robin", "Casey"), ("Lee", "Chen", "Rivera", "Morgan", "Park", "Reed")
            return rng.choice(family) + rng.choice(given) + str(rng.randrange(100, 10000))
        if name == "email":
            return f"mino.{rng.getrandbits(48):012x}@example.test"
        if name == "text":
            length = options.get("length", 24)
            if type(length) is not int or not 1 <= length <= 500:
                invalid("data_generator_invalid", "text.length 必须是 1..500 的整数")
            alphabet = "MinoWeb测试输入abcdefghijkmnpqrstuvwxyz23456789"
            return "".join(rng.choice(alphabet) for _ in range(length))
        if name == "date":
            offset = options.get("day_offset", rng.randrange(0, 3660))
            if type(offset) is not int or not -36500 <= offset <= 36500:
                invalid("data_generator_invalid", "date.day_offset 必须是 -36500..36500 的整数")
            return (date(2030, 1, 1) + timedelta(days=offset)).isoformat()
        lower = options.get("min", 1)
        upper = options.get("max", 1_000_000)
        if type(lower) is not int or type(upper) is not int or lower > upper:
            invalid("data_generator_invalid", "number 的 min/max 必须是有效整数范围")
        return rng.randint(lower, upper)

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
