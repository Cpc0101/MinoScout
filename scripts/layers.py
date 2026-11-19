#!/usr/bin/env python3
"""分层发布的唯一真源：层的定义、指纹、以及安装后的目录布局。

**为什么要分层**：实测冻结产物 1.0 GB（zip ≈ 450 MB），其中浏览器 78%、
运行时 21%、Scout 自己的代码 0.9%（源码 zip 后只有 85 KB）。而每次发版真正
变的只有最后那 0.9%。三层各有独立指纹，客户端只下指纹变了的层。

| 层 | 内容 | 指纹 | 什么时候才会变 |
|---|---|---|---|
| runtime | `mino-scout` 可执行 + `_internal/` | `rt-<hash>` | pyproject 依赖 / Python / 冻结配方变 |
| app     | `app/mino_scout/**.py` 纯源码       | 版本号        | 每次发版 |
| browser | `ms-playwright/**`                  | `bw-<hash>`  | playwright 换 browser revision |

**runtime 层必须原子**：PyInstaller 的 exe 里嵌着 PKG，TOC 引用 `_internal/`
下的具体文件名，两者只能同一次 build 一起换。

**安装布局刻意仍以 `bin/` 为根**：Studio 的 `scoutBinCandidates()` 和 launchd
plist 都硬编码了 `$PREFIX/bin/mino-scout` 与 `$PREFIX/bin/ms-playwright`，
换根目录等于跨仓破坏。app 层挂在 `bin/app`，可执行文件按 `sys.executable`
的同级目录去找 —— 与 `ms-playwright` 已有的做法一致。

刻意零依赖（只用 stdlib），这样 CI 和守门脚本都能用裸 python3 跑。
"""
from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 层名。顺序即安装顺序：runtime 先落地，app 后落地（app 依赖 runtime 存在）。
LAYERS = ("runtime", "app", "browser")

# 首次安装 / 合并包：只带 runtime+app，快速上线；browser 由 Scout 启动后拉取。
BOOTSTRAP_LAYERS = ("runtime", "app")

# zip 内的载荷目录名就是层名：<zip 根>/runtime/、<zip 根>/app/、<zip 根>/browser/。
# 这里是它们 → 安装到 bin/ 下的相对路径。runtime 平铺到 bin/ 根，所以值是 ""。
# **install.sh / install.ps1 里同一份映射是硬编码的** —— 改这里要同步改那两处。
PAYLOAD_DIRS = {
    "runtime": "",
    "app": "app",
    "browser": "ms-playwright",
}

# 冻结产物里属于 app / browser 层的顶层名字，其余都算 runtime。
_APP_DIR = "app"
_BROWSER_DIR = "ms-playwright"

# 冻结配方的世代号。**改了 build_binary.py 的 COLLECT_ALL / EXTRA_HIDDEN /
# EXCLUDES / 入口脚本 / runtime 锁定依赖算法，就要 +1**
# IM 附件增加 ssl/ipaddress hidden imports，旧 runtime 必须随 app 更新。
# 7：hidden-import 扫描保留完整子模块名（修复 html.parser 冻结缺失）。
RUNTIME_ABI = 7


def package_version() -> str:
    text = (ROOT / "mino_scout" / "VERSION").read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("could not read mino_scout/VERSION")
    return text


def declared_dependencies() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = list(data.get("project", {}).get("dependencies", []))
    return sorted(str(d).replace(" ", "") for d in deps)


def locked_dependencies(frozen: Path | None = None) -> list[str]:
    """冻结产物 runtime.lock（build 时 pip freeze）。无则回退 pyproject 声明。"""
    candidates: list[Path] = []
    if frozen is not None:
        candidates.extend([frozen / "runtime.lock", frozen / "_internal" / "runtime.lock"])
    candidates.append(ROOT / "build" / "dist" / "mino-scout" / "runtime.lock")
    for path in candidates:
        if not path.is_file():
            continue
        lines: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if t and not t.startswith("#"):
                lines.append(t.replace(" ", ""))
        if lines:
            return sorted(lines)
    return declared_dependencies()


def chromium_revision_from_frozen(frozen: Path) -> str:
    """Playwright driver 内嵌的 Chromium revision（与 ms-playwright/ 目录必须一致）。"""
    candidates = [
        frozen / "_internal" / "playwright" / "driver" / "package" / "browsers.json",
        frozen / "playwright" / "driver" / "package" / "browsers.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("browsers") or []:
            if str(row.get("name") or "") == "chromium":
                return str(row.get("revision") or "")
    return ""


def runtime_key(
    os_name: str,
    arch: str,
    *,
    python_tag: str = "",
    chromium_revision: str = "",
    frozen: Path | None = None,
) -> str:
    """runtime 层指纹。依赖声明 + Python 次版本 + 冻结世代 + Chromium revision + 平台。"""
    tag = python_tag or f"{sys.version_info.major}.{sys.version_info.minor}"
    rev = str(chromium_revision or "").strip()
    if not rev and frozen is not None:
        rev = chromium_revision_from_frozen(frozen)
    material = "\n".join([
        f"abi={RUNTIME_ABI}",
        f"python={tag}",
        f"os={os_name}",
        f"arch={arch}",
        f"chromium_rev={rev or 'unknown'}",
        *locked_dependencies(frozen),
    ])
    return "rt-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def browser_dir_names(browsers_root: Path) -> list[str]:
    """`ms-playwright/` 下的 revision 目录名，如 chromium-1234。

    playwright 是按 revision 命名的目录找浏览器的，目录名本身就是版本，
    所以多个 revision 可以并存 —— 换 runtime 时旧目录留着也不会打架。
    """
    if not browsers_root.is_dir():
        return []
    return sorted(
        p.name for p in browsers_root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def browser_key(browsers_root: Path) -> str:
    """browser 层指纹：所有 revision 目录名的哈希。

    刻意不只取 chromium 的 revision —— ffmpeg 有自己的 revision（实测 1011），
    可以独立滚动。只看 chromium 会漏掉 ffmpeg 的更新。
    """
    names = browser_dir_names(browsers_root)
    if not names:
        return ""
    return "bw-" + hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:10]


def split_frozen(frozen: Path) -> dict[str, list[Path]]:
    """把冻结产物按层分桶。返回 {层名: [该层的文件绝对路径]}。"""
    buckets: dict[str, list[Path]] = {name: [] for name in LAYERS}
    if not frozen.is_dir():
        return buckets
    for path in sorted(frozen.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(frozen)
        top = rel.parts[0] if rel.parts else ""
        if top == _APP_DIR:
            buckets["app"].append(path)
        elif top == _BROWSER_DIR:
            buckets["browser"].append(path)
        else:
            buckets["runtime"].append(path)
    return buckets


def payload_relpath(layer: str, path: Path, frozen: Path) -> str:
    """文件在 zip 载荷目录内的相对路径。

    runtime 平铺（`mino-scout`、`_internal/...`），app 与 browser 去掉自己那层
    前缀（`app/mino_scout/x.py` → `mino_scout/x.py`），这样 install 脚本可以
    统一按 PAYLOAD_DIRS 的映射整目录替换。
    """
    rel = path.relative_to(frozen)
    if layer == "runtime":
        return rel.as_posix()
    return Path(*rel.parts[1:]).as_posix()
