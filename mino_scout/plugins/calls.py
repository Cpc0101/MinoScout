"""飞书、Meego、机器人的本机调用。密钥从本机私有文件读，不放进回执。"""
from __future__ import annotations

from typing import Any

import httpx

from mino_scout.plugins.host_exec import HostExecError, run_entry
from mino_scout.plugins.state import plain_value, plugin_dir, secret_value

_FEISHU = "https://open.feishu.cn/open-apis"
_MEEGO = "https://project.feishu.cn"


class PluginCallError(Exception):
    def __init__(self, message: str, *, code: Any = None):
        super().__init__(message)
        self.code = code


def _need(node_id: str, kind: str, plugin_id: str, hint: str) -> None:
    from mino_scout.plugins.state import configured

    if not configured(node_id, kind, plugin_id):
        raise PluginCallError(hint)


def feishu_read_doc(node_id: str, url: str) -> dict[str, Any]:
    link = str(url or "").strip()
    if not link:
        raise PluginCallError("url 不能为空")
    from mino_scout.plugins import feishu_auth
    if feishu_auth.ready():
        data = feishu_auth.run_json(["docs", "+fetch", "--doc", link, "--doc-format", "markdown", "--as", "user"])
        import json
        return {"content": json.dumps(data, ensure_ascii=False), "title": "", "source": "cli"}
    _need(node_id, "cli", "feishu", "请在这台 Scout 节点的飞书 CLI 页面完成扫码授权")
    cli = None  # 手动应用凭据使用 OpenAPI；官方 CLI 不支持历史 read_doc 子命令。
    if cli is not None:
        text = _run_cli(node_id, "cli", "feishu", cli, ["read_doc", link])
        if text.strip():
            return {"content": text, "title": "", "source": "cli"}
    app_id = plain_value("cli", "feishu", "app_id")
    app_secret = secret_value(node_id, "cli", "feishu", "app_secret")
    token = _feishu_tenant_token(app_id, app_secret)
    doc_id, title = _feishu_resolve(link, token)
    content = _feishu_raw(doc_id, token)
    return {"content": content, "title": title, "document_id": doc_id, "source": "open_api"}


def feishu_check(node_id: str) -> dict[str, Any]:
    from mino_scout.plugins import feishu_auth
    if feishu_auth.ready():
        return feishu_auth.check()
    _need(node_id, "cli", "feishu", "请在这台 Scout 节点的飞书 CLI 页面完成扫码授权")
    app_id = plain_value("cli", "feishu", "app_id")
    app_secret = secret_value(node_id, "cli", "feishu", "app_secret")
    _feishu_tenant_token(app_id, app_secret)
    return {"ok": True, "app_id": app_id, "summary": f"飞书鉴权通过，App ID {app_id}"}


def meego_call(node_id: str, params: dict[str, Any]) -> dict[str, Any]:
    _need(node_id, "cli", "meego", "请在这台 Scout 节点的 CLI / Meego 填写插件 ID 和插件密钥")
    action = str(params.get("action") or "token").strip()
    base = plain_value("cli", "meego", "base_url") or _MEEGO
    plugin_id = plain_value("cli", "meego", "plugin_id")
    plugin_secret = secret_value(node_id, "cli", "meego", "plugin_secret")
    user_key = plain_value("cli", "meego", "user_key")
    token = _meego_token(base, plugin_id, plugin_secret)
    if action == "token":
        return {"ok": True, "plugin_id": plugin_id, "summary": "Meego 插件凭据有效；尚未校验空间访问和创建工作项权限。"}
    if action != "work_item":
        raise PluginCallError(f"不支持的 Meego 动作 {action}")
    project_key = str(params.get("project_key") or "").strip()
    work_item_id = str(params.get("work_item_id") or "").strip()
    if not project_key or not work_item_id:
        raise PluginCallError("查工作项需要 project_key 和 work_item_id")
    cli = _invoke_bin("cli", "meego")
    if cli is not None:
        text = _run_cli(
            node_id, "cli", "meego", cli,
            ["work_item", project_key, work_item_id],
        )
        return {"ok": True, "content": text, "source": "cli"}
    headers = {"X-PLUGIN-TOKEN": token}
    if user_key:
        headers["X-USER-KEY"] = user_key
    url = f"{base.rstrip('/')}/open_api/{project_key}/work_item/story/query"
    body = {"work_item_ids": [int(work_item_id)] if work_item_id.isdigit() else [work_item_id]}
    data = _json("POST", url, headers=headers, json_body=body)
    if int(data.get("err_code") or data.get("code") or 0) not in (0,):
        raise PluginCallError(str(data.get("err_msg") or data.get("msg") or "Meego 查询失败"))
    return {"ok": True, "data": data.get("data") or {}, "source": "open_api"}


def bot_send(node_id: str, kind: str, text: str) -> dict[str, Any]:
    plugin_id = "feishu_bot" if kind == "feishu_bot" else "wecom" if kind == "wecom" else ""
    if not plugin_id:
        raise PluginCallError("只支持飞书机器人和企业微信")
    hint = "请在这台节点的 Bot 里填写 Webhook"
    _need(node_id, "bot", plugin_id, hint)
    webhook = secret_value(node_id, "bot", plugin_id, "webhook_url")
    if not webhook.startswith("https://"):
        raise PluginCallError(hint)
    body = str(text or "").strip() or "Mino 测试消息"
    if plugin_id == "feishu_bot":
        payload = {"msg_type": "text", "content": {"text": body}}
    else:
        payload = {"msgtype": "text", "text": {"content": body}}
    data = _json("POST", webhook, json_body=payload)
    code = data.get("errcode", data.get("code", data.get("StatusCode")))
    if code not in (0, None, "0"):
        raise PluginCallError(str(data.get("errmsg") or data.get("msg") or "发送失败"))
    return {"ok": True, "kind": plugin_id, "summary": "测试消息已发到群里"}


def _invoke_bin(kind: str, plugin_id: str):
    root = plugin_dir(kind, plugin_id)
    path = root / "invoke"
    if path.is_file():
        return path
    return None


def _run_cli(node_id: str, kind: str, plugin_id: str, entry, argv: list[str]) -> str:
    extra: dict[str, str] = {}
    if plugin_id == "feishu":
        extra["FEISHU_APP_ID"] = plain_value(kind, plugin_id, "app_id")
        extra["FEISHU_APP_SECRET"] = secret_value(node_id, kind, plugin_id, "app_secret")
    elif plugin_id == "meego":
        extra["MEEGO_PLUGIN_ID"] = plain_value(kind, plugin_id, "plugin_id")
        extra["MEEGO_PLUGIN_SECRET"] = secret_value(node_id, kind, plugin_id, "plugin_secret")
    try:
        ran = run_entry(
            entry.parent, entry.name, argv, timeout_sec=90,
            node_id=node_id, kind=kind, plugin_id=plugin_id, extra_env=extra,
        )
    except HostExecError as exc:
        raise PluginCallError(str(exc)) from exc
    if int(ran.get("code") or 0) != 0:
        raise PluginCallError(str(ran.get("stderr") or ran.get("stdout") or "命令失败"))
    return str(ran.get("stdout") or "")


def _feishu_tenant_token(app_id: str, app_secret: str) -> str:
    data = _json(
        "POST",
        f"{_FEISHU}/auth/v3/tenant_access_token/internal",
        json_body={"app_id": app_id, "app_secret": app_secret},
    )
    if data.get("code") != 0:
        raise PluginCallError(f"飞书鉴权失败: {data.get('msg', data)}")
    token = str(data.get("tenant_access_token") or "")
    if not token:
        raise PluginCallError("飞书没有返回访问凭证")
    return token


def _feishu_resolve(url: str, token: str) -> tuple[str, str]:
    import re
    from urllib.parse import urlparse

    wiki = re.search(r"/wiki/([A-Za-z0-9]+)", url, re.I)
    doc = re.search(r"/docx/([A-Za-z0-9]+)", url, re.I)
    if doc:
        return doc.group(1), ""
    if not wiki:
        path = urlparse(url).path or ""
        if "docx" not in path:
            raise PluginCallError("无法识别飞书链接，请使用 /wiki/ 或 /docx/ 地址")
        raise PluginCallError("无法识别飞书文档 id")
    data = _json(
        "GET",
        f"{_FEISHU}/wiki/v2/spaces/get_node",
        headers={"Authorization": f"Bearer {token}"},
        params={"token": wiki.group(1)},
    )
    if data.get("code") != 0:
        raise PluginCallError(f"解析知识库节点失败: {data.get('msg', data)}")
    node = (data.get("data") or {}).get("node") or {}
    if str(node.get("obj_type") or "").lower() != "docx" or not node.get("obj_token"):
        raise PluginCallError("该知识库节点不是 docx 文档")
    return str(node.get("obj_token")), str(node.get("title") or "")


def _feishu_raw(doc_id: str, token: str) -> str:
    data = _json(
        "GET",
        f"{_FEISHU}/docx/v1/documents/{doc_id}/raw_content",
        headers={"Authorization": f"Bearer {token}"},
    )
    if data.get("code") != 0:
        raise PluginCallError(f"读取飞书文档正文失败: {data.get('msg', data)}")
    content = str((data.get("data") or {}).get("content") or "")
    if not content.strip():
        raise PluginCallError("飞书文档正文为空")
    return content


def _meego_token(base: str, plugin_id: str, plugin_secret: str) -> str:
    data = _json(
        "POST",
        f"{base.rstrip('/')}/open_api/authen/plugin_token",
        json_body={"plugin_id": plugin_id, "plugin_secret": plugin_secret, "type": 0},
    )
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = err.get("code", data.get("code", data.get("err_code", 0)))
    if code not in (0, None, "0"):
        raise PluginCallError(str(err.get("msg") or data.get("err_msg") or data.get("msg") or "Meego 鉴权失败"), code=code)
    token = str((data.get("data") or {}).get("token") or data.get("token") or "")
    if not token:
        raise PluginCallError("Meego 没有返回插件凭证")
    return token


def _json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        resp = httpx.request(method, url, headers=headers, params=params, json=json_body, timeout=60)
    except httpx.HTTPError as exc:
        raise PluginCallError(f"请求失败: {exc}") from exc
    try:
        data = resp.json()
    except Exception as exc:
        raise PluginCallError(f"响应不是 JSON（HTTP {resp.status_code}）") from exc
    if not isinstance(data, dict):
        raise PluginCallError("响应不是对象")
    return data
