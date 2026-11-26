## 0.1.172 — 2026-11-26

- Page script failures retain credential-free executor_failure categories and phases in sealed action evidence; JavaScript source, exception messages and page contents are not copied into diagnostics.
- Timeout setup failures before evaluate report execution_started=no. Errors after entering evaluate remain unknown and isolated, including JavaScript SyntaxError; no replay or automatic release.
- CI runs Web interaction smoke checks covering preparation versus execution failures, diagnostic redaction and preserved unknown-write fences.

## 0.1.171 — 2026-11-26

- Note accepts a positional message alongside a text field, preserving legacy keyword messages and sensitive-field filtering.
- Unhandled script exceptions are BLOCKED with script_failed, not product assertion FAIL. Explicit assertion failures remain FAIL.

# Changelog

## 0.1.170 — 2026-11-26

- 新设备写动作在尚未记录 tool/call 的入口最多等待连接恢复 60 秒；持续检查取消、运行期限和已有未知写闩锁。超过期限明确记录 admission / execution_started=no，不重放已开始或结果未知的写操作。恢复连接不等于恢复执行授权：跨进程保留连接改变闩锁，无法重新证明 owner/资源 epoch/generation 时明确 safe-fail；原生 Web 实际写边界再次检查。
- Nexus 回调区分不可达、断线、超时、传输异常、无效应答和业务拒绝；父子进程保留失败类别，单次发送，不自动重试。预算必须有限且在 (0,86400] 秒，拒绝 nan/inf/非正值；核对 RESULT run_id/step_idx 与类型，私有 IPC 请求 ID 隔离迟到回复。
- 连接清理立即结束旧连接的应答等待；注册成功后重置重连退避并立即启动持久摘要同步。
- 连接诊断增加原始异常类别、底层 cause 类别/errno、收发关闭码、连接存活时长和 ping 延迟；不记录凭据。
- record.sync 日志明确 sent_version、ack_version、status、sealed，并区分无应答、拒绝和 ACK 不匹配；仅持久 ACK 对应版本，较新终态仍留 outbox，不影响写隔离。
- 增加隔离离线契约检查，包括 running 748 在途时生成 sealed 833 的 ACK 竞争。验证与发布边界见 docs/CONNECTION_DIAGNOSTICS_0.1.170.md。
