"""Playwright 定位：文案 / CSS / data-testid，与 Nexus params 对齐。"""
from __future__ import annotations

import re
from typing import Any

from mino_scout.platform.web.playwright_context import resolve_locator_scope

# 定价页等站点会在可见文案里插入零宽字符，exact 字符串因此 0 命中。
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_INVISIBLE_GAP = r"[\u200b-\u200f\u2060\ufeff]*"


def normalize_visible_text(value: str) -> str:
    return _INVISIBLE.sub("", str(value or "")).strip()


def exact_visible_pattern(label: str) -> re.Pattern[str] | None:
    """可见文案的精确匹配。字符之间允许零宽字符，不把子串当成命中。"""
    text = normalize_visible_text(label)
    if not text:
        return None
    body = _INVISIBLE_GAP.join(re.escape(ch) for ch in text)
    return re.compile(f"^{_INVISIBLE_GAP}{body}{_INVISIBLE_GAP}$")


def unique_exact_text(scope: Any, label: str) -> tuple[Any | None, int]:
    """只在恰好一个命中时返回 locator。0 与多个都不是可点目标。"""
    pattern = exact_visible_pattern(label)
    if pattern is None or scope is None:
        return None, 0
    try:
        loc = scope.get_by_text(pattern)
        count = int(loc.count())
    except Exception:
        return None, 0
    if count == 1:
        return loc, 1
    return None, count


def _p(params: dict[str, Any] | None) -> dict[str, Any]:
    return dict(params or {})


def css_selector_from_params(params: dict[str, Any] | None) -> str:
    p = _p(params)
    for key in ("css", "selector", "css_selector"):
        val = str(p.get(key) or "").strip()
        if val:
            return val
    tid = str(p.get("test_id") or p.get("data_testid") or p.get("data-testid") or "").strip()
    if tid:
        esc = tid.replace("\\", "\\\\").replace('"', '\\"')
        return f'[data-testid="{esc}"]'
    return ""


def locator_from_params(page: Any, params: dict[str, Any] | None, *, name: str = "") -> Any | None:
    """按 css → testid → 文案角色链解析第一个可用 locator。"""
    p = _p(params)
    scope = resolve_locator_scope(page, p)
    css = css_selector_from_params(p)
    if p.get("require_unique") or p.get("role"):
        from mino_scout.runtime.script_exec import ScriptAbort
        if css:
            loc = scope.locator(css)
        elif p.get("role"):
            pattern = exact_visible_pattern(str(p.get("role_name") or ""))
            if pattern is None:
                raise ScriptAbort("动态对象缺少明确文案", reason_code="locator_missing", status="blocked")
            loc = scope.get_by_role(str(p["role"]), name=pattern)
        else:
            pattern = exact_visible_pattern(str(p.get("selector_text") or name or ""))
            if pattern is None:
                raise ScriptAbort("动态对象缺少明确文案", reason_code="locator_missing", status="blocked")
            loc = scope.get_by_text(pattern)
        count = int(loc.count())
        if count > 1:
            raise ScriptAbort("动态对象有多个候选", reason_code="locator_ambiguous", status="blocked")
        return loc if count == 1 else None
    if css:
        try:
            loc = scope.locator(css)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
    label = normalize_visible_text(str(name or "") or str(p.get("selector_text") or p.get("text") or ""))
    pattern = exact_visible_pattern(label)
    if pattern is None:
        return None
    locators = (
        scope.get_by_role("button", name=pattern),
        scope.get_by_role("link", name=pattern),
        scope.get_by_role("tab", name=pattern),
        scope.get_by_role("menuitem", name=pattern),
        scope.get_by_role("textbox", name=pattern),
        scope.get_by_placeholder(pattern),
        scope.get_by_label(pattern),
        scope.get_by_text(pattern),
    )
    for loc in locators:
        try:
            if int(loc.count()) == 1:
                return loc
        except Exception:
            continue
    return None


def click_locator(loc: Any, *, timeout_ms: int = 4000) -> None:
    loc.click(timeout=timeout_ms)


def input_locators_for_field(
    page: Any,
    login_field: str,
    name: str,
    text: str,
    params: dict[str, Any] | None = None,
) -> list[Any]:
    scope = resolve_locator_scope(page, params)
    candidates: list[Any] = []
    lf = str(login_field or "").strip().lower()
    if lf == "email":
        candidates.extend((
            scope.locator("input[type='email']"),
            scope.locator("input[autocomplete='email']"),
            scope.locator("input[name*='email' i]"),
            scope.get_by_placeholder(re.compile(r"email|邮箱|e-?mail", re.I)),
            scope.get_by_label(re.compile(r"email|邮箱", re.I)),
            scope.get_by_role("textbox", name=re.compile(r"email|邮箱", re.I)),
        ))
    elif lf == "phone":
        candidates.extend((
            scope.locator("input[type='tel']"),
            scope.locator("input[autocomplete='tel']"),
            scope.locator("input[name*='phone' i], input[name*='mobile' i]"),
            scope.get_by_placeholder(re.compile(r"手机|phone|mobile", re.I)),
            scope.get_by_label(re.compile(r"手机|phone", re.I)),
        ))
    elif lf == "sms_code":
        candidates.extend((
            scope.locator("input[autocomplete='one-time-code']"),
            scope.locator("input[name*='otp' i], input[name*='code' i], input[name*='captcha' i]"),
            scope.get_by_placeholder(re.compile(r"验证码|code|otp", re.I)),
            scope.get_by_label(re.compile(r"验证码|code", re.I)),
        ))
    elif lf == "password":
        candidates.extend((
            scope.locator("input[type='password']"),
            scope.get_by_placeholder(re.compile(r"密码|password", re.I)),
        ))
    css = css_selector_from_params({"css": name} if name.startswith(("#", ".", "[")) else {})
    if css:
        candidates.append(scope.locator(css))
    if name and lf not in ("email", "phone", "sms_code", "password"):
        loc = locator_from_params(page, {**(params or {}), "selector_text": name}, name=name)
        if loc is not None:
            candidates.append(loc)
    raw = (text or "").strip()
    if not lf and "@" in raw:
        candidates.extend((
            scope.locator("input[type='email']"),
            scope.locator("input[autocomplete='email']"),
            scope.get_by_placeholder(re.compile(r"email|邮箱", re.I)),
        ))
    if re.fullmatch(r"1\d{10}", raw):
        candidates.extend((
            scope.get_by_placeholder(re.compile(r"手机")),
            scope.locator("input[type='tel']"),
            scope.locator("input[name*='phone' i], input[name*='mobile' i]"),
        ))
    elif re.fullmatch(r"\d{4,8}", raw):
        candidates.extend((
            scope.get_by_placeholder(re.compile(r"验证码")),
            scope.locator("input[autocomplete='one-time-code']"),
        ))
    return candidates
