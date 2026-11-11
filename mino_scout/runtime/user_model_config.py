"""``~/.mino/config.toml``（开发环境也可 ``~/.mino-dev/config.toml``）。

::

    [providers.umodelverse]
    base_url = "https://api.modelverse.cn/v1"
    api_key = "YOUR_API_KEY"
    model = "deepseek-v4-pro"

多 provider 时用 ``[model] provider = "..."`` 或 ``MINO_MODEL_PROVIDER``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _mino_home() -> Path:
    explicit = str(os.environ.get("MINO_CONFIG") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path.parent if path.suffix.lower() == ".toml" else path
    if not getattr(sys, "frozen", False):
        return Path.home() / ".mino-dev"
    return Path.home() / ".mino"


def config_toml_path() -> Path:
    explicit = str(os.environ.get("MINO_CONFIG") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.suffix.lower() == ".toml":
            return path
        return path / "config.toml"
    primary = _mino_home() / "config.toml"
    if primary.is_file():
        return primary
    # 开发态可共用正式目录下的一份，减少重复维护。
    shared = Path.home() / ".mino" / "config.toml"
    if not getattr(sys, "frozen", False) and shared.is_file():
        return shared
    return primary


def load_provider(name: str | None = None) -> dict[str, str]:
    """返回 ``base_url`` / ``api_key`` / ``model``。缺配置抛 ``ValueError``。"""
    path = config_toml_path()
    if not path.is_file():
        raise ValueError(f"未找到模型配置文件：{path}（请按 [providers.*] 编写 config.toml）")
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover
        raise ValueError("当前 Python 缺少 tomllib，无法读取 config.toml") from exc
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解析模型配置 {path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"模型配置格式无效：{path}")
    providers = raw.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError(f"{path} 缺少 [providers.*] 段")
    chosen = (name or "").strip() or str(os.environ.get("MINO_MODEL_PROVIDER") or "").strip()
    model_block = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    if not chosen:
        chosen = str(model_block.get("provider") or "").strip()
    names = [str(k) for k in providers.keys() if str(k).strip()]
    if not chosen:
        if len(names) == 1:
            chosen = names[0]
        else:
            raise ValueError(
                f"{path} 有多个 provider（{', '.join(names)}），"
                "请设置 [model] provider = \"...\" 或环境变量 MINO_MODEL_PROVIDER"
            )
    row = providers.get(chosen)
    if not isinstance(row, dict):
        raise ValueError(f"{path} 没有 [providers.{chosen}]")
    out = {
        "provider": chosen,
        "base_url": str(row.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(row.get("api_key") or "").strip(),
        "model": str(row.get("model") or "").strip(),
    }
    missing = [k for k in ("base_url", "api_key", "model") if not out[k]]
    if missing:
        raise ValueError(f"[providers.{chosen}] 缺少字段：{', '.join(missing)}")
    return out


def require_provider() -> dict[str, str]:
    """给 model_call 用：失败信息面向用例。"""
    try:
        return load_provider()
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
