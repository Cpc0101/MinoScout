"""Authoring files stay in place; execution data belongs to a task/attempt."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

from mino_scout.runtime.records import identifier


def attempt_workspace(store, task_id: str, attempt_id: str) -> Path:
    root = store._folder(task_id) / "attempts" / quote(identifier(attempt_id), safe="-_.")
    if root.is_symlink() or not root.resolve().is_relative_to(store._folder(task_id).resolve()):
        raise ValueError("execution_workspace_invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def authoring_workspace(value: str, execution_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("authoring_workspace_missing")
    path = path.resolve()
    execution_root = execution_root.resolve()
    if path == execution_root or path in execution_root.parents or execution_root in path.parents:
        raise ValueError("authoring_execution_workspace_overlap")
    if not (path / "config.yaml").is_file():
        raise ValueError("config_missing")
    return path


def source_snapshot(root: Path) -> tuple[dict[str, bytes], dict[str, str], str]:
    """Capture code/config and supported data inputs; generated output lives elsewhere."""
    files: dict[str, bytes] = {}
    size = 0
    for directory, children, names in os.walk(root, followlinks=False):
        children[:] = sorted(name for name in children if not name.startswith(".")
                             and name not in {"node_modules", "__pycache__", "venv"}
                             and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix.lower() not in {".py", ".yaml", ".yml", ".json", ".csv", ".tsv", ".txt"} or path.is_symlink():
                continue
            data = path.read_bytes()
            size += len(data)
            if size > 32 * 1024 * 1024 or len(files) >= 10000:
                raise ValueError("workspace_snapshot_too_large")
            files[path.relative_to(root).as_posix()] = data
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return files, hashes, digest
