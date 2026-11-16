"""Static case-isolation checks for MinoScout script repositories.

Every case file must directly own its business flow. Shared ``flows`` and
``scripts`` modules may provide mechanisms or pure calculations, but must not
dispatch cases, receive the whole ``case`` object, or emit Nexus business
events. These checks only parse source code; they never import or execute the
repository under test.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

Finding = tuple[str, int, str, str, str]

CASE_ID_RE = re.compile(r"\bUAT\s*[-_]?\s*\d{2,}\b", re.IGNORECASE)
CASE_TOKEN_RE = re.compile(r"^(?:case|test|tc|uat)[-_]?\d{2,}$", re.IGNORECASE)
CASE_HANDLER_RE = re.compile(r"^_?case[_-]?\d{2,}$", re.IGNORECASE)
CASE_IDENTIFIER_RE = re.compile(r"case[_-]?id|case[_-]?key", re.IGNORECASE)
CASE_DISPATCH_RE = re.compile(
    r"(?:^|[_-])(?:run|execute|dispatch|handle).*case"
    r"|case.*(?:run|execute|dispatch|handle)(?:$|[_-])",
    re.IGNORECASE,
)
NEXUS_EVENT_METHODS = {"prep", "do", "check", "note", "fail", "assert_equal"}
SHARED_DIRS = ("flows", "scripts")
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules"}


def _dotted_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _is_nexus_business_call(call: ast.Call) -> bool:
    name = _dotted_name(call.func)
    if not name:
        return False
    parts = name.split(".")
    return (
        len(parts) >= 3
        and parts[0] == "case"
        and parts[-2] == "nexus"
        and parts[-1] in NEXUS_EVENT_METHODS
    )


def _direct_nexus_calls(node: ast.AST) -> list[ast.Call]:
    """Collect case.nexus calls directly under node, excluding nested defs/lambdas."""
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, item: ast.FunctionDef) -> None:
            if item is node:
                self.generic_visit(item)

        def visit_AsyncFunctionDef(self, item: ast.AsyncFunctionDef) -> None:
            if item is node:
                self.generic_visit(item)

        def visit_Lambda(self, item: ast.Lambda) -> None:
            return

        def visit_Call(self, item: ast.Call) -> None:
            if _is_nexus_business_call(item):
                calls.append(item)
            self.generic_visit(item)

    Visitor().visit(node)
    return calls


def _string_literals(tree: ast.AST) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, getattr(node, "lineno", 1)


def _imported_shared_helpers(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return local helper names and module aliases imported from flows/scripts."""
    helpers: set[str] = set()
    module_aliases: set[str] = set()
    shared_prefixes = tuple(directory + "." for directory in SHARED_DIRS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in SHARED_DIRS or module.startswith(shared_prefixes):
                for alias in node.names:
                    if alias.name != "*":
                        helpers.add(alias.asname or alias.name)
                        if module in SHARED_DIRS:
                            module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in SHARED_DIRS or alias.name.startswith(shared_prefixes):
                    module_aliases.add(alias.asname or alias.name.split(".", 1)[0])
    return helpers, module_aliases


def _call_is_imported_shared_helper(
    call: ast.Call, helpers: set[str], module_aliases: set[str]
) -> bool:
    name = _dotted_name(call.func)
    if not name:
        return False
    return name in helpers or name.split(".", 1)[0] in module_aliases


def _call_passes_whole_case(call: ast.Call) -> bool:
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(isinstance(value, ast.Name) and value.id == "case" for value in values)


def _identifies_case(value: str, case_keys: set[str]) -> bool:
    normalized = value.strip().casefold()
    return bool(
        CASE_ID_RE.search(value)
        or CASE_TOKEN_RE.match(normalized)
        or normalized in case_keys
    )


def _call_has_case_id(call: ast.Call, case_keys: set[str]) -> bool:
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and _identifies_case(value.value, case_keys)
        for value in values
    )


def _run_has_direct_stop(run: ast.AST) -> bool:
    """Detect an explicit blocked/fail path directly inside run(case)."""
    found = False

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is run:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is run:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Raise(self, node: ast.Raise) -> None:
            nonlocal found
            name = _dotted_name(node.exc.func if isinstance(node.exc, ast.Call) else node.exc)
            if name and name.rsplit(".", 1)[-1] in {"ScriptAbort", "AssertionError", "RuntimeError"}:
                found = True
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            nonlocal found
            name = _dotted_name(node.func)
            if name and name.endswith(".nexus.fail"):
                found = True
            self.generic_visit(node)

    Visitor().visit(run)
    return found


def _has_module_assignment(tree: ast.Module, name: str) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return True
    return False


def _find_run(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            return node
    return None


def _run_signature_issue(run: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    args = run.args
    positional = [*args.posonlyargs, *args.args]
    if isinstance(run, ast.AsyncFunctionDef):
        return "run(case) must be synchronous"
    if len(positional) != 1 or args.vararg or args.kwarg or args.kwonlyargs:
        return "run() must have exactly one positional parameter named 'case' and no other parameters"
    if positional[0].arg != "case":
        return "run() positional parameter must be named 'case'"
    if args.defaults:
        return "run(case) must not define a default for 'case'"
    return None


def _check_case_file(path: Path, tree: ast.Module, case_keys: set[str]) -> list[Finding]:
    findings: list[Finding] = []

    # ``path`` is absolute. The caller rewrites paths relative to the repository
    # root after all findings are collected.
    def add(line: int, message: str) -> None:
        findings.append((str(path), line, "case_isolation", message, "error"))

    if not _has_module_assignment(tree, "CASE_SOURCE"):
        add(1, "missing module-level CASE_SOURCE; source mapping is not directly reviewable")

    run = _find_run(tree)
    if run is None:
        add(1, "missing module-level run(case); the file cannot run as an independent case")
        return findings

    signature_issue = _run_signature_issue(run)
    if signature_issue:
        add(run.lineno, signature_issue)

    direct_calls = _direct_nexus_calls(run)
    direct_events = {
        (name.rsplit(".", 1)[-1])
        for call in direct_calls
        if (name := _dotted_name(call.func))
    }
    direct_stop = _run_has_direct_stop(run)

    if "prep" not in direct_events:
        add(run.lineno, "run(case) does not directly call case.nexus.prep(); preparation cannot be hidden in a shared helper")
    if "do" not in direct_events and not direct_stop:
        add(run.lineno, "run(case) does not directly call case.nexus.do() or define a case-local blocked/fail path")
    if "check" not in direct_events and not direct_stop:
        add(run.lineno, "run(case) does not directly call case.nexus.check() or define a case-local blocked/fail path")

    direct_call_ids = {id(call) for call in direct_calls}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if _is_nexus_business_call(call) and id(call) not in direct_call_ids:
            add(call.lineno, "case.nexus business events must be written directly in run(case), not in a nested or module-level helper")

    helpers, module_aliases = _imported_shared_helpers(tree)
    for call in (node for node in ast.walk(run) if isinstance(node, ast.Call)):
        name = _dotted_name(call.func) or ""
        imported_helper = _call_is_imported_shared_helper(call, helpers, module_aliases)
        if _call_has_case_id(call, case_keys) and (
            imported_helper
            or name.startswith("run_")
            or "case" in name.lower()
            or "handler" in name.lower()
        ):
            add(call.lineno, "run(case) delegates by case ID to a centralized handler; case business logic must stay in this file")
        if imported_helper and CASE_DISPATCH_RE.search(name):
            add(call.lineno, f"run(case) calls centralized shared dispatcher '{name}'; case business logic must stay in this file")
        if imported_helper and _call_passes_whole_case(call):
            add(call.lineno, "whole case object is passed to a flows/scripts helper; pass only app, case.scout, page objects, or plain values")

    for node in ast.walk(tree):
        name = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if name and CASE_HANDLER_RE.match(name):
            add(node.lineno, f"case file must not define centralized case handler '{name}'; write the business flow in run(case)")
        if isinstance(node, ast.Name) and CASE_IDENTIFIER_RE.search(node.id):
            add(node.lineno, f"case file contains case-ID dispatch variable '{node.id}'; write the business flow directly in run(case)")
    return findings


def _check_shared_file(path: Path, tree: ast.Module, case_keys: set[str]) -> list[Finding]:
    findings: list[Finding] = []

    def add(line: int, message: str) -> None:
        findings.append((str(path), line, "case_isolation", message, "error"))

    for text, line in _string_literals(tree):
        if _identifies_case(text, case_keys):
            add(line, f"shared module contains case identifier {text!r}; flows/scripts must not dispatch by case ID")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if CASE_HANDLER_RE.match(node.name):
                add(node.lineno, f"shared module contains centralized case handler '{node.name}'")
            if CASE_IDENTIFIER_RE.search(node.name):
                add(node.lineno, f"shared function name contains case-ID dispatch semantics: '{node.name}'")
            parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for parameter in parameters:
                if parameter.arg in {"case", "case_obj"}:
                    add(parameter.lineno or node.lineno, f"shared function must not accept the whole case object: {node.name}({parameter.arg})")
        elif isinstance(node, ast.Name) and CASE_IDENTIFIER_RE.search(node.id):
            add(node.lineno, f"shared module contains case-ID dispatch variable: '{node.id}'")
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func) or ""
            if ".nexus." in f".{name}." and name.rsplit(".", 1)[-1] in NEXUS_EVENT_METHODS:
                add(node.lineno, f"shared module directly calls '{name}'; events, business assertions, and blocked decisions belong in case files")
    return list(dict.fromkeys(findings))


def validate_case_isolation(root: Path, trees: dict[Path, ast.Module]) -> list[Finding]:
    """Return case-isolation findings using the common check finding tuple."""
    root = Path(root).resolve()
    case_paths = sorted(
        path
        for path in trees
        if "tests" in path.relative_to(root).parts and path.name != "__init__.py"
    )
    if not case_paths:
        return []

    case_keys = {path.stem.strip().casefold() for path in case_paths}
    findings: list[Finding] = []
    for path in case_paths:
        findings.extend(_check_case_file(path, trees[path], case_keys))

    for directory in SHARED_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root)
            if path.name == "__init__.py" or SKIP_DIRS.intersection(rel.parts) or path not in trees:
                continue
            findings.extend(_check_shared_file(path, trees[path], case_keys))

    return [
        (str(Path(path).relative_to(root)), line, code, text, severity)
        for path, line, code, text, severity in dict.fromkeys(findings)
    ]
