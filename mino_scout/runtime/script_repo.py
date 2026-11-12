"""脚本仓库：Scout 自己拉取项目的用例仓库，在指定提交上取出一份干净副本。

Nexus 只下发仓库地址、分支和（可选）提交，不打包、不检查、不保存令牌。私有仓库用本机
git 自己的凭据（SSH agent / credential helper）。取不到时返回错误码，不弹认证。
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

_GIT_TIMEOUT = 180
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,120}$")
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class RepoError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def checkout(repo_url: str, branch: str = "", revision: str = "") -> tuple[Path, str]:
    """返回（副本目录, 实际提交）。revision 为空取分支最新；调用方用完删除目录。"""
    url = str(repo_url or "").strip()
    if not url or url.startswith("-"):
        raise RepoError("script_repo_missing", "没有脚本仓库地址")
    branch = str(branch or "").strip()
    revision = str(revision or "").strip()
    for name, value in (("分支", branch), ("提交", revision)):
        if value and not _SAFE_REF.match(value):
            raise RepoError("repo_unavailable", f"{name}名不合法")
    cache = _cache_dir(url)
    with _lock(str(cache)):
        _sync(url, cache)
        sha = _resolve(cache, branch, revision)
        data = _archive(cache, sha)
    root = Path(tempfile.mkdtemp(prefix="mino-script-"))
    try:
        _extract(data, root)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root, sha


def locate_script(root: Path, case_id: str, script_path: str = "") -> str:
    """用例脚本的仓库内相对路径。给了路径就用它，否则找 tests/**/<case_id>.py。"""
    base = Path(root).resolve()
    given = str(script_path or "").strip().replace("\\", "/")
    if given:
        path = (base / given).resolve()
        if base not in path.parents or path.suffix != ".py" or not path.is_file():
            raise RepoError("script_missing", f"仓库里没有 {given}")
        return path.relative_to(base).as_posix()
    cid = str(case_id or "").strip()
    tests = base / "tests"
    found = sorted(p for p in tests.rglob(f"{cid}.py") if p.is_file()) if tests.is_dir() and cid else []
    if not found:
        raise RepoError("script_missing", f"仓库里没有 tests/<需求ID>/{cid}.py")
    if len(found) > 1:
        shown = ", ".join(p.relative_to(base).as_posix() for p in found[:8])
        raise RepoError("script_ambiguous", f"tests 里有多份 {cid}.py：{shown}")
    return found[0].relative_to(base).as_posix()


_SCRIPT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def list_requirement(root: Path, requirement_id: str) -> list[dict[str, str]]:
    """tests/<需求ID> 目录下直接放置的用例脚本。渠道后缀和不合法文件名跳过。"""
    rid = str(requirement_id or "").strip()
    if not _SCRIPT_ID.fullmatch(rid):
        raise RepoError("script_path_invalid", "需求 ID 不合法")
    base = Path(root).resolve()
    folder = (base / "tests" / rid).resolve()
    if base not in folder.parents:
        raise RepoError("script_path_invalid", "需求目录超出仓库")
    if not folder.is_dir():
        return []
    rows: list[dict[str, str]] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix != ".py" or not _SCRIPT_ID.fullmatch(path.stem):
            continue
        resolved = path.resolve()
        if base not in resolved.parents:
            continue
        rows.append({"case_id": path.stem, "script_path": resolved.relative_to(base).as_posix()})
    return rows


def _cache_dir(url: str) -> Path:
    from mino_scout.config import config_dir

    return config_dir() / "script-repos" / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".git")


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, env=_ENV,
                              capture_output=True, timeout=_GIT_TIMEOUT)
    except FileNotFoundError as exc:
        raise RepoError("repo_unavailable", "本机没有安装 git") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError("repo_unavailable", "git 操作超时") from exc


def _tail(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return (text[-1] if text else "")[:200]


def _sync(url: str, cache: Path) -> None:
    if (cache / "HEAD").is_file():
        proc = _git(["fetch", "--prune", "--force", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], cwd=cache)
        if proc.returncode != 0:
            raise RepoError("repo_unavailable", f"拉取仓库失败（检查本机 git 凭据）：{_tail(proc)}")
        return
    shutil.rmtree(cache, ignore_errors=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    proc = _git(["clone", "--bare", "--", url, str(cache)])
    if proc.returncode != 0:
        shutil.rmtree(cache, ignore_errors=True)
        raise RepoError("repo_unavailable", f"克隆仓库失败（检查地址和本机 git 凭据）：{_tail(proc)}")


def _resolve(cache: Path, branch: str, revision: str) -> str:
    ref = revision or (f"refs/heads/{branch}" if branch else "HEAD")
    proc = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cache)
    sha = (proc.stdout or b"").decode().strip()
    if proc.returncode != 0 or not sha:
        if revision:
            raise RepoError("revision_missing", f"仓库里取不到提交 {revision}，确认已 push")
        raise RepoError("repo_unavailable", f"仓库里没有分支 {branch or 'HEAD'}")
    return sha


def _archive(cache: Path, sha: str) -> bytes:
    proc = _git(["archive", "--format=tar", sha], cwd=cache)
    if proc.returncode != 0:
        raise RepoError("repo_unavailable", f"导出提交失败：{_tail(proc)}")
    return proc.stdout


def _extract(data: bytes, root: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts or not (member.isreg() or member.isdir()):
                continue
            tar.extract(member, root, filter="data")
