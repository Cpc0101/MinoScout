# Scout 0.1.170 连接诊断与发布交接

日期：2026-11-26。修改范围仅 MinoScout 源码；未 commit、push、tag、部署、安装、更新或重启本机 Scout。

## 历史证据

来源：`~/.mino/scout/logs/scout.err.log`，日志是 Asia/Shanghai，不是 UTC。

| 断线 UTC | 恢复注册 UTC | 断线至注册 |
|---|---|---|
| 09:53:50.246 | 09:54:20.353 | 30.107 秒 |
| 09:59:06.655 | 09:59:46.312 | 39.657 秒 |
| 10:04:21.277 | 10:05:03.538 | 42.261 秒 |

三次均为 `ConnectionClosedError: no close frame received or sent`，同一 Scout PID 62232，无重启证据。该窗口没有 heartbeat send stalled 或等待应答超时的日志。

**已确认的机制**：WebSocket 未完成关闭握手即结束；原实现将每次已成功注册后的异常仍计入连续拨号失败，使重连退避增至 24.3/34.4/35.0 秒；脚本新写入口遇到断线立即 blocked。回调无应答被统一归类为 nexus_rejected，无法据此认定服务端拒绝。

**尚未确认的底层原因**：历史日志没有 TCP errno、异常 cause、ping/pong 诊断、代理或 Nexus 同时段关闭日志，不能判定究竟是代理超时、网络重置、服务器退出还是其他原因。三次间隔约 315 秒不构成代理超时的证明。本次补充诊断供下一次连接失败取证，不宣称已修复外部网络/代理故障。

## sealed / record.sync 审计

- 通过只读 SQLite 查询审计本机 573 个 attempt；当时均为终态，outbox 为空，未发现 summary_version > acked_version 且 outbox 缺失的记录。该检查只说明 Scout 的 ACK 状态，不证明 Nexus 任务投影。
- 通过 Mino MCP task_get 核对 `CR261126100415821::EMB-002::webe22615fbbb917111`：Nexus 当前为 fail、sealed=true、summary_version=834、last_event_seq=833；本地亦为 fail、summary_version=834、acked_version=834。**833 是该记录当前事件序号，不是当前摘要版本**。历史运行中 748 的 record_sync 原始事件未由当前接口返回，不能凭现态重建历史投递时间线。
- `resource_log_list(action=record_sync)` 对该任务返回空；它不是可用的摘要同步事件证据。补充的 Scout 日志今后明确 sent_version/ack_version/status/sealed，而不向本地事件表追加同步日志，避免日志本身提高摘要版本导致无限同步。
- Nexus `loop/scout_record_tasks.receive` 在索引持久化后独立尝试 project，投影异常只记 projection pending，仍返回 ACK。若索引已 ACK 但任务投影滞后，责任是 Nexus 的持久投影重试；不能让 Scout 删除 ACK、自动重跑设备或将 ACKed 终态无条件重新入队。未改 Nexus，未读取 Nexus 数据库。
- 保留现有 outbox 事务和旋转公平性；旧 ACK 748 只能删除 <=748 的 outbox，较新 sealed 833 保留。新增 smoke 在 running 748 发出后、ACK 返回前生成 sealed 833，随后验证 748 ACK 不清掉 833，833 ACK 才清除；未知写隔离标志保留。

## 实现边界

**本版本不是无缝恢复：不存在可验证 owner/epoch/generation 的既有重绑定契约，断线后的原任务不能自动续写，仍 fail-closed 中断。60 秒等待仅是有界观察连接恢复，不授予新的执行身份，也不消除中断。主线程可据此启动三平台构建，但不能发布“自动续写/无缝恢复”的声明。**


- 等待只发生在新写动作 admission、tool/call 之前，最长 60 秒；取消/脚本期限和已有未知写闩锁随时终止。没有设备动作重试，没有未知写重放。恢复 event 不能证明执行授权：原生 Web 脚本直调 Playwright，并非每次经过 BindingGuard。现以跨进程 connection_changed 不可清除闩锁识别代际变化，包括两次动作之间已断线又恢复的情形；无法再次证明 owner/资源 epoch/generation 时 node_connection_revalidation_required / execution_started=no，禁止盲目续写。原生 Web 在控件预检后、实际 click/fill 或鼠标写入前再次检查；router 的写停止闩也包含连接改变/断线。
- 断线清理结束旧连接等待；callback 错误经私有 IPC 传递，不增加 wire 协议字段。REGISTER / EXECUTE / RESULT 协议未改，无需两仓协议同步。
- 成功注册重置拨号退避；注册完成即启动摘要同步，仍只发送 record.sync。
- 运行服务仍使用已安装冻结包 0.1.169，仓库版本变化不作用于运行服务。

## 已执行验证

- `python3 scripts/check_connection_contracts.py`：通过。新写 admission/重连、取消/期限检查、未知写闩锁；单次回调与错误分类，nan/inf/非正/超大预算拒绝、RESULT run_id/step_idx/类型校验、IPC 迟到回复隔离；真实 asyncio 请求超时/断线及 waiter 清理；旧/错/畸形 ACK、终态隔离、748→833 竞争、旋转公平性。
- `python3 scripts/check_case_lifecycle.py`：通过。debug 队列、生命周期、空 check、原文覆盖、清理、失败保留、回调分类、数据摘要、OTP 边界和脱敏。
- `python3 scripts/check_web_auth_contracts.py`：通过。
- `python3 scripts/check_web_interaction_contracts.py`：通过。
- `python3 scripts/check_web_observation_contracts.py`：通过。
- `python3 -m compileall -q mino_scout scripts`：通过。
- `bash -n packaging/bootstrap.sh packaging/install.sh`：通过。
- `git diff --check`：通过。
- 版本一致性：VERSION、core.SCOUT_VERSION、layers.package_version、pack_release.package_version、app_version 的版本读取均为 0.1.170；隔离临时目录生成 setuptools dist-info，METADATA Version=0.1.170。pyproject 动态版本唯一来源为 mino_scout/VERSION，CHANGELOG 同版本。
- `mino-scout status`：运行 PID 62232、已安装 0.1.169、heartbeat/registered；仅只读查询。
- `python3 scripts/check_windows_lifecycle.py`：当前 macOS 提示须 Windows release runner 执行，**未通过 Windows 验证**。

未运行 pytest，未新增/修改 tests/test_*.py。上述离线 smoke 不是设备实测、线上验收或发布成功。

## 主线程发布检查清单（未执行）

1. 审查修改，固定 main 提交并创建 v0.1.170 tag；本任务不提交/tag/push。
2. 原生 runner 执行三平台冻结与 probe；Windows 生命周期/PowerShell 语法/原生 SDK 检查不能由 macOS smoke 替代。
3. 用固定 clean build 生成 app/runtime/browser/合并包与 manifest；各包内 VERSION、app layer、manifest.version 和 tag 一致为 0.1.170。禁止把 dirty 工作区包标成正式 validated build。
4. 更新包体与 SHA256、build_id/source_commit/dirty=false 校验；未修改依赖或冻结配方，无需人为提高 runtime ABI。
5. 若做分层安装 smoke，仅隔离临时安装根并 MINO_SCOUT_SKIP_SERVICE=1；禁止更新或重启本机 Scout。
6. 发布后的网络/投影回归另行取证；本次不预填 rollback/update 实测兼容证据。
