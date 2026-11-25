"""写完用例后的自检：只读源码，给 agent 提示，不参与执行。

执行时不做任何预检，脚本写成什么样都会跑。这里列出的问题是：绕开 Scout 的写法、
自己再造 Scout / Nexus 已有的能力、旧的或写错的 case 用法。每条带文件、行号和改法。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from mino_scout.runtime.script_exec import _BANNED, parse_yaml

_NO_CONCURRENCY = {"threading", "multiprocessing", "asyncio", "signal", "concurrent"}
_REUSE = {
    "imaplib": "prepare case.scout.otp_request(account) before sending mail codes, then use case.scout.otp(account, request=request)",
    "poplib": "prepare case.scout.otp_request(account) before sending mail codes, then use case.scout.otp(account, request=request)",
    "openai": "use case.scout.llm() / app.vlm() for model calls",
    "anthropic": "use case.scout.llm() / app.vlm() for model calls",
}
_CASE_ATTRS = {"config", "env", "app_id", "app_key", "init", "scout", "nexus", "data", "workspace", "output_dir"}
_STEP_CALLS = {"prep", "do", "check"}
_EMPTY_PREP = {"无", "无前置", "无需前置", "不需要", "不需要前置", "无操作", "none", "n/a", "na"}
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


@dataclass
class Hint:
    path: str
    line: int
    code: str
    text: str
    severity: str = "error"

    def __str__(self) -> str:
        label = self.code if self.severity == "error" else f"{self.code}/{self.severity}"
        return f"{self.path}:{self.line}: [{label}] {self.text}"


def check_repo(root: Path, *, require_cases: bool = False) -> list[Hint]:
    root = Path(root).resolve()
    hints: list[Hint] = []
    keys = _config_keys(root, hints)
    used: dict[str, tuple[str, int]] = {}
    trees: dict[Path, ast.Module] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except SyntaxError as exc:
            hints.append(Hint(str(rel), int(exc.lineno or 1), "syntax", exc.msg))
            continue
        except (OSError, UnicodeError) as exc:
            hints.append(Hint(str(rel), 0, "source_unreadable", str(exc)))
            continue
        trees[path] = tree
        for line, code, text in _scan(tree):
            hints.append(Hint(str(rel), line, code, text))
        if "tests" in rel.parts:
            hints.extend(_prep_required(tree, str(rel)))
            from mino_scout.runtime.account_literals import find_account_literals
            for line, kind in find_account_literals(source):
                hints.append(Hint(str(rel), line, "hardcoded_account",
                    f"Do not hardcode {kind}: rent the account via case.nexus.account(...) and read fields from it; "
                    "add `# account-literal-ok: <reason>` only when the text is page copy under assertion"))
        for key, line in _config_uses(tree):
            used.setdefault(key, (str(rel), line))
    if keys is not None:
        for key, (rel, line) in sorted(used.items()):
            if key not in keys:
                hints.append(Hint(rel, line, "config_missing", f"config.yaml apps does not define {key}"))
    hints.extend(_page_layout(root, keys))
    from mino_scout.runtime.script_validation import validate_sources
    hints.extend(Hint(*row) for row in validate_sources(root, trees, require_cases=require_cases))
    from mino_scout.runtime.case_isolation import validate_case_isolation
    hints.extend(Hint(*row) for row in validate_case_isolation(root, trees))
    from mino_scout.runtime.model_policy import validate_model_usage
    hints.extend(Hint(*row) for row in validate_model_usage(root, trees))
    return hints


def _scan(tree: ast.AST) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        line = int(getattr(node, "lineno", 0) or 0)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [str(node.module or "")]
            for name in names:
                top = name.split(".", 1)[0]
                if top in _BANNED:
                    out.append((line, "forbidden", f"Do not import {top}: use app.* for device actions and app.exec() for device commands"))
                elif top in _NO_CONCURRENCY:
                    out.append((line, "forbidden", f"Do not import {top}: blocked threads/processes can stall the entire node; run steps sequentially"))
                elif top in _REUSE:
                    out.append((line, "reuse", f"Do not reimplement this: {_REUSE[top]}"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"exec", "eval", "__import__"}:
                out.append((line, "forbidden", f"Do not call {func.id}(): use app.exec() for device commands"))
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                owner, attr = func.value.id, func.attr
                if owner == "os" and attr in {"system", "popen"}:
                    out.append((line, "forbidden", f"Do not call os.{attr}(): use app.exec() for device commands"))
                elif owner == "time" and attr == "sleep":
                    out.append((line, "reuse", "Do not use time.sleep: use case.scout.wait(ms) so cancellation and timeouts are respected"))
                elif owner == "case" and attr == "init":
                    keywords_ok = all(k.arg == "web_resolution" for k in node.keywords)
                    if len(node.args) != 1 or not _is_config_ref(node.args[0]) or not keywords_ok:
                        out.append((line, "case_init",
                                    'case.init accepts case.config.<KEY> and optional web_resolution="720P|1080P|2K|3K/4K" only'))
            if isinstance(func, ast.Attribute) and func.attr in _STEP_CALLS and _is_nexus(func.value):
                if not _titled(node):
                    out.append((line, "step_title", f"case.nexus.{func.attr}() needs a concrete step title; it becomes the report step name"))
                elif func.attr == "prep" and _blank_prep(node):
                    out.append((line, "prep_reason", "Preparation title cannot be empty filler such as 'none'. State what was done, or why no preparation is needed"))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "case":
            if node.attr not in _CASE_ATTRS:
                out.append((line, "case_attr", f"case.{node.attr} does not exist: use app = case.init(case.config.<KEY>) for device actions, case.scout for node capabilities, and case.nexus for Nexus events"))
        elif isinstance(node, ast.If) and _mentions_platform(node.test):
            out.append((line, "forbidden", "Do not branch on platform: the same script must run on every channel; put differences in pages locator_spec"))
    return out


def _is_nexus(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "nexus" and isinstance(node.value, ast.Name) and node.value.id == "case"


def _titled(node: ast.Call) -> bool:
    arg = node.args[0] if node.args else None
    return isinstance(arg, ast.Constant) and isinstance(arg.value, str) and bool(arg.value.strip())


def _blank_prep(node: ast.Call) -> bool:
    arg = node.args[0] if node.args else None
    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
        return False
    return arg.value.strip().lower() in _EMPTY_PREP


def _prep_required(tree: ast.AST, rel: str) -> list[Hint]:
    """tests/ 下的用例文件必须写 prep。没有动作时，标题写明为什么。"""
    runs = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "run"]
    if not runs:
        return []
    calls = [node for node in ast.walk(tree) if _is_prep_call(node)]
    if any(_titled(node) and not _blank_prep(node) for node in calls):
        return []
    if calls:
        return []
    line = int(runs[0].lineno or 1)
    return [Hint(rel, line, "prep_missing",
                 "Add case.nexus.prep(\"what was done\"). If preparation is unnecessary, state why; this title is recorded in Nexus")]


def _is_prep_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr == "prep" and _is_nexus(node.func.value)


def _is_config_ref(node: ast.AST | None) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "config" and isinstance(node.value.value, ast.Name) and node.value.value.id == "case")


def _config_uses(tree: ast.AST) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(tree):
        if _is_config_ref(node):
            out.append((node.attr, int(node.lineno)))
    return out


def _mentions_platform(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "platform":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "platform":
            return True
    return False


def _config_keys(root: Path, hints: list[Hint]) -> set[str] | None:
    path = root / "config.yaml"
    if not path.is_file():
        hints.append(Hint("config.yaml", 0, "config_missing", "Repository root does not contain config.yaml"))
        return None
    try:
        doc = parse_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        hints.append(Hint("config.yaml", 0, "config_invalid", f"Unable to parse config.yaml: {exc}"))
        return None
    apps = doc.get("apps") if isinstance(doc, dict) else None
    if not isinstance(apps, dict) or not apps:
        hints.append(Hint("config.yaml", 0, "config_invalid", "Top-level apps must be an object containing at least one application"))
        return None
    for key, row in apps.items():
        if not isinstance(row, dict) or not str(row.get("platform") or ""):
            hints.append(Hint("config.yaml", 0, "config_invalid", f"{key} is missing platform"))
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,31}$", str(key)):
            hints.append(Hint("config.yaml", 0, "config_invalid", f"Application key {key} must start with an ASCII letter and contain only letters, digits, and underscores"))
        if isinstance(row, dict):
            hints.extend(_otp_hints(str(key), row))
            envs = row.get("envs")
            if isinstance(envs, dict):
                for env, block in envs.items():
                    if isinstance(block, dict):
                        hints.extend(_otp_hints(f"{key}.envs.{env}", block))
    return {str(key) for key in apps}


def _otp_hints(label: str, row: dict) -> list[Hint]:
    if "otp" not in row:
        return []
    otp = row.get("otp")
    if otp is None:
        return []
    if not isinstance(otp, dict):
        return [Hint("config.yaml", 0, "config_invalid", f"{label}.otp must be an object whose only allowed field is code")]
    unknown = [str(k) for k in otp if str(k) != "code"]
    out: list[Hint] = []
    if unknown:
        out.append(Hint("config.yaml", 0, "config_invalid",
                        f"{label}.otp only allows code; unknown fields: {', '.join(unknown)}"))
    if "code" in otp and otp.get("code") is not None and not isinstance(otp.get("code"), str):
        out.append(Hint("config.yaml", 0, "config_invalid", f"{label}.otp.code must be a string"))
    return out


def _page_layout(root: Path, keys: set[str] | None) -> list[Hint]:
    folder = root / "pages"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in {".yaml", ".yml"} or not path.is_file():
            continue
        parts = path.relative_to(folder).parts
        rel = str(path.relative_to(root))
        if len(parts) < 2:
            out.append(Hint(rel, 0, "page_layout", "Page files must be under pages/<APP_KEY>/"))
        elif keys is not None and parts[0] not in keys:
            out.append(Hint(rel, 0, "page_layout", f"Directory {parts[0]} is not an application key in config.yaml"))
    return out
