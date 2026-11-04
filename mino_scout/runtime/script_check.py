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
    "imaplib": "取验证码用 case.scout.otp_mail()",
    "poplib": "取验证码用 case.scout.otp_mail()",
    "openai": "调模型用 case.scout.llm() / app.vlm()",
    "anthropic": "调模型用 case.scout.llm() / app.vlm()",
}
_CASE_ATTRS = {"config", "env", "app_id", "init", "scout", "nexus"}
_STEP_CALLS = {"prep", "do", "check"}
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


@dataclass
class Hint:
    path: str
    line: int
    code: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.code}] {self.text}"


def check_repo(root: Path) -> list[Hint]:
    root = Path(root).resolve()
    hints: list[Hint] = []
    keys = _config_keys(root, hints)
    used: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if _SKIP_DIRS.intersection(rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # 语法错误 agent 自己看得到，这里不重复
        for line, code, text in _scan(tree):
            hints.append(Hint(str(rel), line, code, text))
        for key, line in _config_uses(tree):
            used.setdefault(key, (str(rel), line))
    if keys is not None:
        for key, (rel, line) in sorted(used.items()):
            if key not in keys:
                hints.append(Hint(rel, line, "config_missing", f"config.yaml 的 apps 里没有 {key}"))
    hints.extend(_page_layout(root, keys))
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
                    out.append((line, "forbidden", f"不要 import {top}：设备动作走 app.*，设备命令走 app.exec()"))
                elif top in _NO_CONCURRENCY:
                    out.append((line, "forbidden", f"不要 import {top}：线程/进程挂住会拖垮整个节点，顺序写"))
                elif top in _REUSE:
                    out.append((line, "reuse", f"不要自己实现：{_REUSE[top]}"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"exec", "eval", "__import__"}:
                out.append((line, "forbidden", f"不要调用 {func.id}()：设备命令走 app.exec()"))
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                owner, attr = func.value.id, func.attr
                if owner == "os" and attr in {"system", "popen"}:
                    out.append((line, "forbidden", f"不要调用 os.{attr}()：设备命令走 app.exec()"))
                elif owner == "time" and attr == "sleep":
                    out.append((line, "reuse", "不要 time.sleep：用 case.scout.wait(ms)，它会响应取消和超时"))
                elif owner == "case" and attr == "init":
                    if not _is_config_ref(node.args[0] if len(node.args) == 1 and not node.keywords else None):
                        out.append((line, "case_init", "case.init 的参数必须是 case.config.<KEY>"))
            if isinstance(func, ast.Attribute) and func.attr in _STEP_CALLS and _is_nexus(func.value):
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.strip()):
                    out.append((line, "step_title", f"case.nexus.{func.attr}() 要写这一步具体做什么的文字标题，它就是测试报告里的步骤名"))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "case":
            if node.attr not in _CASE_ATTRS:
                out.append((line, "case_attr", f"case.{node.attr} 不存在：设备动作写在 app = case.init(case.config.<KEY>) 的 app 上，本机能力 case.scout，Nexus 能力 case.nexus"))
        elif isinstance(node, ast.If) and _mentions_platform(node.test):
            out.append((line, "forbidden", "不要按 platform 分支：同一份脚本要在所有渠道跑，差异放进 pages 的 locator_spec"))
    return out


def _is_nexus(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "nexus" and isinstance(node.value, ast.Name) and node.value.id == "case"


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
        hints.append(Hint("config.yaml", 0, "config_missing", "仓库根目录没有 config.yaml"))
        return None
    try:
        doc = parse_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        hints.append(Hint("config.yaml", 0, "config_invalid", f"解析失败：{exc}"))
        return None
    apps = doc.get("apps") if isinstance(doc, dict) else None
    if not isinstance(apps, dict) or not apps:
        hints.append(Hint("config.yaml", 0, "config_invalid", "顶层要有 apps，且至少一个应用"))
        return None
    for key, row in apps.items():
        if not isinstance(row, dict) or not str(row.get("platform") or ""):
            hints.append(Hint("config.yaml", 0, "config_invalid", f"{key} 缺 platform"))
        elif not str(row.get("app_id") or ""):
            hints.append(Hint("config.yaml", 0, "config_invalid", f"{key} 缺 app_id（Nexus 里这个应用的 id，见 project_list）"))
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,31}$", str(key)):
            hints.append(Hint("config.yaml", 0, "config_invalid", f"应用 key {key} 要以英文字母开头，只含英文、数字、下划线"))
    return {str(key) for key in apps}


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
            out.append(Hint(rel, 0, "page_layout", "页面文件要放在 pages/<APP_KEY>/ 下"))
        elif keys is not None and parts[0] not in keys:
            out.append(Hint(rel, 0, "page_layout", f"目录 {parts[0]} 不是 config.yaml 里的应用 key"))
    return out
