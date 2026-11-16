"""Static cross-file checks. Never import or execute code from a script repository."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

Finding = tuple[str, int, str, str, str]
_CONTROLS = {"tap", "input", "long_press"}
_ROLES = {"prep": "preconditions", "do": "steps", "check": "expectations"}


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _scope(call: ast.AST) -> str:
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return ""
    obj = call.func.value
    if isinstance(obj, ast.Attribute) and obj.attr == "nexus" and isinstance(obj.value, ast.Name) and obj.value.id == "case":
        return call.func.attr if call.func.attr in _ROLES else ""
    return ""


def _read_yaml(path: Path) -> dict[str, Any]:
    from mino_scout.runtime.script_exec import parse_yaml
    value = parse_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("YAML top level must be an object")
    return value


def _module_path(root: Path, file: Path, node: ast.ImportFrom) -> Path:
    base = root if not node.level else file.parent
    for _ in range(max(0, node.level - 1)):
        base = base.parent
    target = base.joinpath(*(node.module or "").split("."))
    package = target / "__init__.py"
    return package if package.is_file() else target if target.is_dir() else target.with_suffix(".py")


def _exports(tree: ast.Module) -> tuple[set[str], bool]:
    names: set[str] = set()
    dynamic = False
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            dynamic |= node.name == "__getattr__"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            dynamic |= any(alias.name == "*" for alias in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        else:
            pending.extend(ast.iter_child_nodes(node))
    return names, dynamic


def _assertion_calls(body: list[ast.stmt]) -> tuple[bool, bool]:
    """Known assertion versus unresolved helper; never execute the helper to decide."""
    possible, unresolved = False, False
    pending = list(body)
    known_observations = {"screenshot", "hierarchy", "note", "wait", "sample", "tap", "input", "launch", "close", "kill", "press", "swipe", "foreground"}
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in {"on_page", "expect", "assert_equal"}:
                possible = True
            elif name in {"vlm", "vlm_frame"}:
                verdict = next((kw.value for kw in node.keywords if kw.arg == "verdict"), None)
                if _literal(verdict) == "pass":
                    possible = True
                elif verdict is not None and not isinstance(verdict, ast.Constant):
                    unresolved = True
            elif name not in known_observations:
                unresolved = True
        pending.extend(ast.iter_child_nodes(node))
    return possible, unresolved


def _app_bindings(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "init"
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "case" and len(call.args) == 1):
            continue
        arg = call.args[0]
        if not (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Attribute)
                and arg.value.attr == "config" and isinstance(arg.value.value, ast.Name) and arg.value.value.id == "case"):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                if target.id in found and found[target.id] != arg.attr:
                    ambiguous.add(target.id)
                found[target.id] = arg.attr
    return {name: key for name, key in found.items() if name not in ambiguous}


def validate_sources(root: Path, trees: dict[Path, ast.Module], *, require_cases: bool = False) -> list[Finding]:
    findings: list[Finding] = []

    def add(path: Path, line: int, code: str, message: str, severity: str = "error") -> None:
        findings.append((str(path.relative_to(root)), line, code, message, severity))

    pages: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted((root / "pages").rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            continue
        parts = path.relative_to(root / "pages").parts
        if len(parts) < 2:
            continue
        try:
            page = _read_yaml(path)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            add(path, 1, "page_invalid", str(exc))
            continue
        page_key = str(page.get("page_key") or path.stem)
        key = (parts[0], page_key)
        if key in pages:
            add(path, 1, "page_duplicate", f"Application {key[0]} defines page {key[1]} more than once")
        pages[key] = page

    case_count = 0
    for path, tree in trees.items():
        rel = path.relative_to(root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                first = (node.module or "").split(".")[0]
                if not node.level and not (root / first).exists() and not (root / (first + ".py")).is_file():
                    if first not in {"flows", "scripts"}:
                        continue
                target = _module_path(root, path, node)
                try:
                    target.relative_to(root)
                except ValueError:
                    add(path, node.lineno, "import_outside_repo", "Relative import resolves outside the script repository")
                    continue
                if target.is_dir():
                    for alias in node.names:
                        if alias.name != "*" and not (target / (alias.name + ".py")).is_file() and not (target / alias.name).is_dir():
                            add(path, node.lineno, "import_symbol_missing", f"{node.module} does not contain module {alias.name}")
                elif not target.is_file():
                    add(path, node.lineno, "import_missing", f"Local module does not exist: {node.module or '.'}")
                elif target in trees:
                    exported, dynamic = _exports(trees[target])
                    for alias in node.names:
                        if alias.name == "*" or dynamic or alias.name in exported:
                            continue
                        if target.name == "__init__.py" and (target.parent / (alias.name + ".py")).is_file():
                            continue
                        add(path, node.lineno, "import_symbol_missing", f"{node.module} does not define {alias.name}")

        apps = _app_bindings(tree)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            fn = call.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name) or fn.value.id not in apps:
                continue
            app_key = apps[fn.value.id]
            if fn.attr in _CONTROLS and call.args:
                control = _literal(call.args[0])
                if control and "." in control:
                    page_key, control_id = control.split(".", 1)
                    page = pages.get((app_key, page_key))
                    if page is None:
                        add(path, call.lineno, "page_missing", f"Application {app_key} does not define page {page_key}")
                        continue
                    controls = [item for item in page.get("controls", []) if isinstance(item, dict)
                                and (item.get("id") == control_id or item.get("label") == control_id)]
                    if len(controls) != 1:
                        add(path, call.lineno, "control_unknown", f"{app_key}/{control} must resolve to exactly one control")
                    elif not controls[0].get("locator_spec"):
                        add(path, call.lineno, "locator_missing", f"{app_key}/{control} has no device-observed locator; collect it before verification")
            elif fn.attr == "on_page" and call.args:
                page_key = _literal(call.args[0])
                if page_key and (app_key, page_key) not in pages:
                    add(path, call.lineno, "page_missing", f"Application {app_key} does not define page {page_key}")

        if "tests" not in rel.parts or path.name == "__init__.py":
            continue
        case_count += 1
        runs = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"]
        if len(runs) != 1 or isinstance(runs[0], ast.AsyncFunctionDef):
            add(path, 1, "entry_missing", "Each case needs exactly one synchronous def run(case) entry point")
        elif len(runs[0].args.posonlyargs + runs[0].args.args) != 1 or runs[0].args.kwonlyargs or runs[0].args.vararg or runs[0].args.kwarg:
            add(path, runs[0].lineno, "entry_signature", "Case entry signature must be run(case)")

        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(_scope(item.context_expr) == "check" for item in node.items):
                effective = [part for part in node.body if not isinstance(part, ast.Pass)
                             and not (isinstance(part, ast.Expr) and isinstance(part.value, ast.Constant))]
                if not effective:
                    add(path, node.lineno, "check_empty", "A check block must verify its expectation and produce an assertion; a title alone is insufficient")
                else:
                    possible, unresolved = _assertion_calls(node.body)
                    if not possible:
                        add(path, node.lineno, "check_assertion_unresolved" if unresolved else "check_no_assertion",
                            "Whether this check's helper produces an assertion must be verified at runtime" if unresolved else "Screenshots, notes, and passing native asserts do not register an expectation; use on_page/expect/vlm(verdict='pass')/assert_equal",
                            "warning" if unresolved else "error")

        spec_path = path.with_suffix(".yaml")
        calls = [node for node in ast.walk(tree) if _scope(node)]
        refs = [kw.value for call in calls for kw in call.keywords if kw.arg == "source_ref"]
        from mino_scout.runtime.script_source import source_binding
        try:
            binding = source_binding(tree)
        except ValueError as exc:
            add(path, 1, "source_invalid", str(exc))
            continue
        if binding is not None:
            if spec_path.is_file():
                add(path, 1, "source_conflict", "CASE_SOURCE and a same-name YAML snapshot cannot coexist; remove the duplicate snapshot")
            add(path, 1, "source_remote", "The Feishu source is read before execution; static checks only validate source configuration, while item mapping and coverage require runtime verification", "warning")
            continue
        if not spec_path.is_file():
            if refs:
                add(path, 1, "source_missing", "source_ref requires CASE_SOURCE or a legacy same-name .yaml snapshot")
            continue
        try:
            spec = _read_yaml(spec_path)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            add(spec_path, 1, "source_invalid", str(exc))
            continue
        if str(spec.get("case_id") or "") != path.stem:
            add(spec_path, 1, "source_case_mismatch", "Source snapshot case_id must equal the script filename")
        source = spec.get("source")
        if not isinstance(source, dict) or not str(source.get("revision") or "").strip():
            add(spec_path, 1, "source_revision_missing", "source.revision must pin the source version or content hash")
        ids: dict[str, str] = {}
        for group in _ROLES.values():
            rows = spec.get(group, [])
            if not isinstance(rows, list):
                add(spec_path, 1, "source_invalid", f"{group} must be a list of items")
                continue
            if group == "expectations" and not rows:
                add(spec_path, 1, "expectations_missing", "A formal case must declare at least one source expectation")
            for row in rows:
                key = str(row.get("id") or "") if isinstance(row, dict) else ""
                if not key or not isinstance(row, dict) or not str(row.get("text") or "").strip():
                    add(spec_path, 1, "source_invalid", f"Every item in {group} needs id and source text")
                elif key in ids:
                    add(spec_path, 1, "source_duplicate", f"Duplicate source item ID {key}")
                else:
                    ids[key] = group
        seen: set[str] = set()
        dynamic = False
        for call in calls:
            ref_node = next((kw.value for kw in call.keywords if kw.arg == "source_ref"), None)
            ref = _literal(ref_node)
            if ref_node is not None and ref is None:
                dynamic = True
                add(path, call.lineno, "source_dynamic", "A dynamic source_ref cannot be confirmed statically; verify coverage at runtime", "warning")
            elif ref:
                if ref not in ids:
                    add(path, call.lineno, "source_unknown", f"Source does not contain item {ref}")
                elif ids[ref] != _ROLES[_scope(call)]:
                    add(path, call.lineno, "source_role_mismatch", f"{ref} does not match the current prep/do/check role")
                else:
                    seen.add(ref)
        missing = set(ids) - seen
        if missing:
            add(path, 1, "source_unmapped", "Source items have no static implementation mapping: " + ", ".join(sorted(missing)), "warning" if dynamic else "error")
    if require_cases and case_count == 0:
        findings.append(("tests", 0, "cases_missing", "No case scripts were found; passing static configuration checks does not mean testing is complete", "error"))
    return findings
