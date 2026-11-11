"""节点指令和具名插件能力。密钥只在本进程里读，不写进回执。"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from mino_scout.server_comm import protocol as P
from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.plugins.gmail_otp import GmailOtpError, fetch_otp_via_imap
from mino_scout.plugins.install import PluginInstallError, install, remove
from mino_scout.plugins.secret_store import SecretStoreError
from mino_scout.plugins.state import apply_config, configured, plain_value, secret_value, status_list
from mino_scout.schemas import EventResult, EventStatus

TAG = "PluginService"
_GMAIL_HINT = "请在这台 Scout 节点的「邮箱 / Gmail」里填写收件箱和应用专用密码"


def _blocked_plugin(core: Any, event: Any, started: str) -> EventResult:
    message = str(getattr(core, "_plugin_warning", "") or "节点正在确认插件账号归属，请稍后重试")
    return make_event_result(
        event,
        status=EventStatus.BLOCKED,
        executor_used="core",
        started_at=started,
        elapsed_ms=0,
        summary=message,
        error=message,
    )


def handle_node_plugin(core: Any, req: P.Execute, cmd: str, event: Any, started: str) -> EventResult:
    if not getattr(core, "_plugins_ready", False):
        return _blocked_plugin(core, event, started)
    params = dict(req.params or {})
    kind = str(params.get("class") or params.get("kind") or "").strip()
    plugin_id = str(params.get("id") or params.get("plugin_id") or "").strip()
    SLog.i(TAG, f"{cmd} {kind}/{plugin_id} node={core.node_id}")
    try:
        if cmd == "plugin_config":
            if kind in ("im", "model") or plugin_id in ("wechat", "langbot", "llm"):
                raise ValueError("模型配置请写 ~/.mino/config.toml")
            values = params.get("values") if isinstance(params.get("values"), dict) else {}
            clear = [str(x) for x in (params.get("clear") or []) if str(x).strip()]
            values = {k: v for k, v in values.items() if k not in ("im_hold", "platform")}
            apply_config(core.node_id, kind, plugin_id, values, clear)
            summary = "已保存" if configured(core.node_id, kind, plugin_id) else "已保存，还有必填项空着"
        elif cmd == "plugin_install":
            if kind in ("im", "model") or plugin_id in ("wechat", "langbot", "llm"):
                raise ValueError("微信 / IM / LangBot 安装已移除")
            summary = install(core.node_id, kind, plugin_id)
        elif cmd == "plugin_remove":
            summary = remove(core.node_id, kind, plugin_id)
        else:
            summary = f"不支持的插件指令 {cmd}"
            return _fail(event, started, summary, cmd)
    except (PluginInstallError, SecretStoreError, ValueError, OSError) as exc:
        return _fail(event, started, str(exc), cmd)
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=0,
        summary=summary,
        raw_response={
            "command": cmd,
            "class": kind,
            "id": plugin_id,
            "configured": configured(core.node_id, kind, plugin_id),
            "plugins": status_list(core.node_id),
        },
    )


def executor_manifest(*, ready: bool) -> P.ExecutorManifest:
    """Finite implementation surface; configuration is reported separately."""
    return P.ExecutorManifest(id="host_plugin", available=bool(ready),
        provides=["plugin.gmail.fetch_otp"] if ready else [],
        reason="" if ready else "plugin_owner_unverified")


def handle_plugin_cap(core: Any, req: P.Execute, cap: str) -> EventResult:
    params = dict(req.params or {})
    named = str(params.get("action") or params.get("capability_id") or params.get("op") or "")
    if cap == "script_run" or named == "script_run":
        return _fail(_event(req), now_iso(), "这个入口不能提交 script_run", cap)
    if not getattr(core, "_plugins_ready", False):
        return _blocked_plugin(core, _event(req), now_iso())
    event = _event(req)
    started = now_iso()
    if cap.startswith("plugin.im.") or cap in ("plugin.bot.wechat",):
        return _fail(event, started, "微信与 IM 对话能力已移除", cap)
    if cap == "plugin.gmail.fetch_otp":
        try:
            result = _gmail(core, req, event, started)
        except Exception:
            return _fail(event, started, 'otp_plugin_failed', cap)
        if req.binding_id:
            result.executor_used = "host_plugin"
            result.raw_response = {**(result.raw_response or {}), "execution_binding": {
                "binding_id": req.binding_id, "operation_ref": req.operation_ref}}
        return result
    try:
        if cap in ("plugin.cli.feishu", "plugin.cli.meego"):
            from mino_scout.plugins.state import _item
            if _item("cli", cap.rsplit(".", 1)[-1]).get("import_blocked"):
                raise ValueError("导入配置已停用，请先安装插件并在配置页启用")
        if cap in ("plugin.cli.feishu", "plugin.cli.meego") and (req.params or {}).get('action') == 'command':
            from mino_scout.plugins.cli_bridge import call
            data = call(core.node_id, cap.rsplit('.', 1)[-1], dict(req.params or {}))
        elif cap == "plugin.cli.feishu":
            data = _feishu(core, req)
        elif cap == "plugin.cli.meego":
            from mino_scout.plugins.calls import meego_call

            data = meego_call(core.node_id, dict(req.params or {}))
        elif cap == "plugin.bot.send":
            from mino_scout.plugins.calls import bot_send

            params = dict(req.params or {})
            kind = str(params.get("kind") or "")
            if kind == "wechat":
                raise ValueError("微信能力已移除")
            data = bot_send(core.node_id, kind, str(params.get("text") or ""))
        else:
            return _fail(event, started, f"这台节点还没有能力 {cap}", cap)
    except Exception as exc:
        from mino_scout.plugins.calls import PluginCallError

        text = str(exc)
        if not isinstance(exc, (PluginCallError, RuntimeError, ValueError)):
            text = f"调用失败: {type(exc).__name__}"
        return _fail(event, started, text, cap)
    data = dict(data)
    data["plugins"] = status_list(core.node_id)
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=0,
        summary=str(data.get("summary") or "完成"),
        raw_response=_public_payload(data),
    )


def _feishu(core: Any, req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins.calls import feishu_check, feishu_read_doc

    params = dict(req.params or {})
    action = str(params.get("action") or "read_doc")
    if action in {"messages_start", "messages_status", "messages_stop"}:
        from mino_scout.plugins import feishu_messages
        return feishu_messages.action(action)
    if action in {"auth_start", "auth_status", "auth_cancel"}:
        from mino_scout.plugins.feishu_auth import action as auth_action
        return auth_action(action)
    if action not in {"check", "read_doc"}:
        raise ValueError("不支持的飞书操作")
    if action == "check":
        data = feishu_check(core.node_id)
        data["summary"] = "飞书应用凭证可用"
        return data
    data = feishu_read_doc(core.node_id, str(params.get("url") or ""))
    data["summary"] = "已读取飞书文档"
    return data


def _wechat(core: Any, req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins import wechat as wx

    wx.bind_node(core.node_id)
    params = dict(req.params or {})
    action = str(params.get("action") or "status")
    if action == "qr":
        data = wx.start_qr()
    elif action == "status":
        data = wx.poll()
    elif action == "verify":
        data = wx.poll(str(params.get("verify_code") or ""))
    elif action == "logout":
        data = wx.logout()
    elif action == "send":
        wx.send_text(
            to_user_id=str(params.get("to_user_id") or ""),
            context_token=str(params.get("context_token") or ""),
            text=str(params.get("text") or ""),
        )
        data = {"ok": True, "summary": "已发送"}
    else:
        raise RuntimeError(f"不支持的微信动作 {action}")
    data.setdefault("summary", "微信状态已更新" if action != "send" else "已发送")
    data.pop("bot_token", None)
    return data


def _im_senders() -> None:
    """渠道模块 import 时各自注册发送函数。这里确保都已加载。"""
    from mino_scout.plugins import langbot_host, wechat  # noqa: F401


def _im_target(params: dict[str, Any]) -> tuple[str, str, str, dict[str, str]]:
    channel = str(params.get("channel") or "").strip()
    chat_id = str(params.get("chat_id") or "").strip()
    sender_id = str(params.get("sender_id") or "").strip()
    raw = params.get("reply_ctx") if isinstance(params.get("reply_ctx"), dict) else {}
    return channel, chat_id, sender_id, {str(k): str(v) for k, v in raw.items()}


def _feishu_cli_bot_send(node_id: str, chat_id: str, text: str, delivery_id: str) -> dict[str, Any]:
    """No active inbound listener: send with the app bot through the official CLI."""
    from mino_scout.plugins.cli_bridge import call

    data = call(node_id, "feishu", {
        "action": "command",
        "mode": "write",
        "command": ["im", "+messages-send"],
        "options": {
            "chat-id": chat_id,
            "msg-type": "text",
            "content": {"text": text},
            "idempotency-key": hashlib.sha256(delivery_id.encode()).hexdigest()[:48],
        },
    })
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {"ok": True, "provider_message_id": str(data.get("id") or inner.get("message_id") or "")}


def _im_send(req: P.Execute, node_id: str = "") -> dict[str, Any]:
    from mino_scout.plugins import im_bridge, im_store

    _im_senders()
    params = dict(req.params or {})
    channel, chat_id, sender_id, ctx = _im_target(params)
    send = im_bridge.text_sender(channel)
    if send is None and channel != "feishu_cli":
        raise ValueError(f"不支持的渠道 {channel or '(空)'}")
    text = str(params.get("text") or "")
    delivery_id = str(params.get("delivery_id") or f"out-{uuid.uuid4().hex}")
    card = params.get('card') or {}
    if channel == "feishu_cli":
        from mino_scout.plugins import feishu_messages

        def operation():
            try:
                return feishu_messages.send(chat_id, ctx, text, delivery_id)
            except ValueError as exc:
                # Reply path needs the inbound listener. For an explicit chat_id,
                # fall back to the bot CLI so proactive push is not reply-only.
                if not chat_id or str(exc) not in (
                    "CLI 消息路由已失效，请重新确认连接",
                    "CLI 应用已变化，拒绝向旧会话发送",
                ):
                    raise
                return _feishu_cli_bot_send(node_id, chat_id, text, delivery_id)
    elif channel == 'feishu' and im_bridge.sender_capabilities(channel).get('cards') and card:
        from mino_scout.plugins import im_feishu
        operation = lambda: im_feishu.send(node_id, chat_id, sender_id, ctx, text, card, delivery_id)
    else:
        operation = lambda: send(chat_id, sender_id, ctx, text)
    sent = im_store.send_once(channel, str(ctx.get("instance_id") or ""), str(ctx.get("tenant") or ""), sender_id,
                             delivery_id, text, card, operation)
    try:
        im_store.record(
            channel, chat_id or sender_id, sender_id, delivery_id, "assistant", text.strip(),
            int(time.time()), instance_id=str(ctx.get("instance_id") or ""),
            tenant=str(ctx.get("tenant") or ""), thread_id=str(ctx.get("thread_id") or ""),
        )
    except Exception as exc:  # 已发出去了，落库失败不回报失败
        SLog.w(TAG, f"im.send 已发送但落库失败: {type(exc).__name__}")
    SLog.i(TAG, f"plugin.im.send channel={channel} len={len(text)}")
    return sent


def _im_attachment(req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins import im_store
    params = dict(req.params or {})
    channel, _, sender, ctx = _im_target(params)
    return im_store.read_attachment({"channel": channel, "instance_id": ctx.get("instance_id", ""),
                                    "tenant": ctx.get("tenant", ""), "sender_id": sender}, str(params.get("ref") or ""))


def _im_ack(req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins import im_store
    params = dict(req.params or {})
    channel, _, sender, ctx = _im_target(params)
    im_store.acknowledge(channel, str(ctx.get("instance_id") or ""), str(ctx.get("tenant") or ""), sender, str(params.get("delivery_key") or ""))
    return {"ok": True, "summary": "已确认受理"}


_IMAGE_MAX = 10 * 1024 * 1024


def _im_upload_image(req: P.Execute) -> dict[str, Any]:
    """发图片。渠道没有图片能力就明确失败，不伪造成功。"""
    import base64
    import mimetypes
    from pathlib import Path

    from mino_scout.plugins import im_bridge

    _im_senders()
    params = dict(req.params or {})
    channel, chat_id, sender_id, ctx = _im_target(params)
    send = im_bridge.image_sender(channel)
    if send is None:
        if im_bridge.text_sender(channel) is None:
            raise ValueError(f"不支持的渠道 {channel or '(空)'}")
        raise ValueError(f"渠道 {channel} 暂不支持发送图片")
    path = str(params.get("path") or "").strip()
    b64 = str(params.get("image_base64") or "").strip()
    mime = "image/png"
    if b64:
        if b64.startswith("data:") and "," in b64:
            head, b64 = b64.split(",", 1)
            mime = head[5:].split(";", 1)[0] or mime
        data = base64.b64decode(b64, validate=False)
    elif path:
        fp = Path(path).expanduser()
        if not fp.is_file():
            raise ValueError("图片文件不存在")
        if fp.stat().st_size > _IMAGE_MAX:
            raise ValueError("图片超过 10MB")
        data = fp.read_bytes()
        mime = mimetypes.guess_type(fp.name)[0] or mime
    else:
        raise ValueError("缺少 image_base64 或 path")
    if not data or len(data) > _IMAGE_MAX:
        raise ValueError("图片为空或超过 10MB")
    send(chat_id, sender_id, ctx, data, mime)
    SLog.i(TAG, f"plugin.im.upload_image channel={channel} bytes={len(data)}")
    return {"ok": True, "summary": "图片已发送"}


def _apply_hold(kind: str, plugin_id: str, hold: Any, platform: str) -> None:
    """Nexus 持有仲裁：im_hold=false 停该渠道（不登出 / 不删配置），true 恢复。"""
    want = hold if isinstance(hold, bool) else str(hold).strip().lower() in ("1", "true", "yes", "on")
    if kind in ("bot", "im") and plugin_id == "wechat":
        from mino_scout.plugins import wechat

        wechat.set_hold(want)
    elif kind == "cli" and plugin_id == "feishu":
        from mino_scout.plugins import feishu_messages
        from mino_scout.plugins.state import remember_plain
        remember_plain("cli", "feishu", {"im_hold": "" if want else "0"})
        feishu_messages.reconcile()
    elif kind == "im" and plugin_id == "langbot":
        from mino_scout.plugins import langbot_host

        langbot_host.set_hold(platform or None, want)
    else:
        raise ValueError(f"{kind}/{plugin_id} 不支持 im_hold")


def _im_history(req: P.Execute) -> dict[str, Any]:
    from mino_scout.plugins import im_store

    params = dict(req.params or {})
    channel = str(params.get("channel") or "").strip()
    chat_id = str(params.get("chat_id") or "").strip()
    if not channel or not chat_id:
        raise ValueError("缺少 channel 或 chat_id")
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    _, _, sender_id, ctx = _im_target(params)
    msgs = im_store.history(channel, chat_id, limit, sender_id=sender_id,
        instance_id=str(ctx.get("instance_id") or ""), tenant=str(ctx.get("tenant") or ""),
        after_ts=int(params.get("after_ts") or 0))
    return {"messages": msgs, "summary": f"{len(msgs)} 条"}


def _im_result(event: Any, started: str, cap: str, data: dict[str, Any]) -> EventResult:
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=0,
        summary=str(data.get("summary") or "完成"),
        raw_response=_public_payload(data),
    )


def _public_payload(data: dict[str, Any]) -> dict[str, Any]:
    hidden = {"bot_token", "app_secret", "plugin_secret", "webhook_url", "app_password"}
    return {k: v for k, v in data.items() if k not in hidden and "secret" not in k and "token" not in k}


def _gmail(core: Any, req: P.Execute, event: Any, started: str) -> EventResult:
    t0 = time.time()
    params = dict(req.params or {})
    import math
    bound = bool(req.execution_contract_version)
    end = time.monotonic() + max(0.0, float(req.timeout_sec))
    def stopped():
        return (time.monotonic() >= end or bool(core._requests.stopped(req.run_id))
                or (bound and bool(core.binding_guard.validate(req)))
                or not getattr(core, '_plugins_ready', False))
    if bound:
        try:
            recipient = params.get('to_address')
            since = float(params['since_ts'])
            deadline = float(params['deadline_ts'])
            wait = int(params['max_wait_ms'])
            if (not isinstance(recipient, str) or '@' not in recipient
                    or any(c.isspace() for c in recipient)
                    or not math.isfinite(since) or since <= 0 or since > time.time()
                    or not math.isfinite(deadline) or deadline <= time.time()
                    or wait <= 0 or wait > 90_000 or stopped()):
                raise ValueError('otp_request_invalid')
        except (KeyError, TypeError, ValueError, OverflowError):
            return _fail(event, started, 'otp_request_invalid', 'plugin.gmail.fetch_otp', t0=t0)
    inbox = plain_value("mail", "gmail", "inbox_address")
    password = secret_value(core.node_id, "mail", "gmail", "app_password")
    if not inbox or not password:
        return _fail(event, started, _GMAIL_HINT, "plugin.gmail.fetch_otp")
    since = params.get("since_ts")
    try:
        since_ts = float(since) if since else None
    except (TypeError, ValueError):
        since_ts = None
    allow = params.get("from_allowlist")
    if not isinstance(allow, list):
        allow = []
    deadline = params.get("deadline_ts")
    try:
        deadline_ts = float(deadline) if deadline else None
    except (TypeError, ValueError):
        deadline_ts = None
    try:
        diagnostics = {}
        code = fetch_otp_via_imap(
            inbox_address=inbox,
            app_password=password,
            to_address=str(params.get("to_address") or ""),
            since_ts=since_ts,
            from_allowlist=[str(x) for x in allow],
            subject_contains=str(params.get("subject_contains") or ""),
            poll_interval_ms=int(params.get("poll_interval_ms") or 200),
            max_wait_ms=int(params.get("max_wait_ms") or 90_000),
            deadline_ts=min(deadline_ts or float("inf"), time.time() + max(0.0, end - time.monotonic())),
            should_stop=stopped, diagnostics=diagnostics,
        )
    except GmailOtpError as exc:
        text = str(exc)
        if "未配置" in text:
            text = _GMAIL_HINT
        result = _fail(event, started, text, "plugin.gmail.fetch_otp", t0=t0)
        result.raw_response['otp_diagnostics'] = {k: v for k, v in diagnostics.items()
            if k in {'connection_attempts', 'reconnects', 'error_code', 'phase'}}
        return result
    if stopped() or (deadline_ts and time.time() >= deadline_ts):
        return _fail(event, started, 'otp_wait_stopped', 'plugin.gmail.fetch_otp', t0=t0)
    return make_event_result(
        event,
        status=EventStatus.PASS,
        executor_used="core",
        started_at=started,
        elapsed_ms=int((time.time() - t0) * 1000),
        summary="已取到验证码",
        raw_response={"code": code, "source": "gmail", "otp_diagnostics": diagnostics},
    )


def _event(req: P.Execute) -> Any:
    from mino_scout.core import _event_from_execute

    return _event_from_execute(req)


def _fail(event: Any, started: str, summary: str, command: str, *, t0: float | None = None) -> EventResult:
    elapsed = 0 if t0 is None else int((time.time() - t0) * 1000)
    return make_event_result(
        event,
        status=EventStatus.FAIL,
        executor_used="core",
        started_at=started,
        elapsed_ms=elapsed,
        summary=summary,
        error=summary,
        raw_response={"command": command},
    )


def _im_stream(req: P.Execute, node_id: str) -> dict:
    from mino_scout.plugins import im_bridge, im_store, im_feishu
    params = dict(req.params or {})
    channel, chat_id, sender_id, ctx = _im_target(params)
    if channel != 'feishu' or not im_bridge.sender_capabilities(channel).get('streaming'):
        raise ValueError('当前适配器不支持流式消息')
    key = str(params.get('stream_id') or '')
    sequence = int(params.get('sequence') or 0)
    if not key or sequence < 1:
        raise ValueError('流式消息缺少稳定编号或版本')
    complete = params.get('complete') is True
    text = str(params.get('text') or '')[:10000]
    card = params.get('card') if complete else {'card_type': 'dialogue', 'header': {'title': '回复草稿 · 尚未执行'}}
    origin = {'channel': channel, 'sender_id': sender_id, **ctx}
    result = im_store.stream_once(origin, key, sequence, text, card or {}, complete,
        lambda mid: im_feishu.update(node_id, mid, text, card, draft=not complete) if mid else im_feishu.send(node_id, chat_id, sender_id, ctx, text, card or {}, key))
    if complete:
        im_store.record(channel, chat_id, sender_id, key, 'assistant', text, instance_id=ctx.get('instance_id', ''), tenant=ctx.get('tenant', ''), thread_id=ctx.get('thread_id', ''))
    return result
