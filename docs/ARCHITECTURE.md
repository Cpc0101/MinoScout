# ARCHITECTURE — MinoScout

## 1. 在整体里的位置

```
┌──────────┐   HTTP :10104   ┌───────────────────────┐   WS /node   ┌──────────────────┐
│ Electron │ ──────────────► │      MinoNexus        │ ◄──────────  │    MinoScout     │
│    UI    │ ◄── WS 推送 ──── │  大脑：循环 / LLM /    │   (Scout     │  手脚：设备操作   │
└──────────┘                 │  能力目录 / 数据库     │    主动连)    └────────┬─────────┘
                             └───────────────────────┘                       │
                                                              adb / WDA / Playwright / macOS·Windows
                                                                             │
                                                                    ┌────────▼─────────┐
                                                                    │  Android / iOS   │
                                                                    │  / 浏览器        │
                                                                    └──────────────────┘
```

**UI 只跟 Nexus 说话。** Scout 不对 UI 暴露任何接口。

邮箱验证码只从 MIME 解码后的主题与可见正文解析。带验证码语义的主题有唯一数字时，必须由正文确认；主题与正文冲突或主题候选不唯一时拒绝。无主题确认时，正文仍须有唯一数字和验证码标签，不能把版权年份或订单编号当成验证码。收件人、发件人、时间窗口、请求边界和邮件消费去重照常生效。

脚本回调 Nexus 被拒绝时，失败摘要优先保留 Nexus 的具体拒绝信息，状态为 `blocked`，原因保留 `nexus_rejected`；无连接为 `blocked/nexus_unreachable`，不计入断言失败。完整 `run/finished` 事件包含 `nexus_error`（能力、操作、上游错误代码），经过出口脱敏且不附回调参数或账号凭据；仅有异常类型时才显示 `ValueError` 等类型名称。

## 2. 四层套娃里 Scout 占哪两层

上游 MiniOrangeServer 的执行是四层：

| 层 | 归属 |
|---|---|
| ① 任务编排（多设备、多用例、闸门、落库） | Nexus |
| ② 单用例循环（observe → decide → dispatch） | **Nexus**（决策在这层） |
| ③ 动作分发（选 executor） | **拆开**：选路在 Nexus，执行在 Scout |
| ④ 设备动作 | **Scout** |

③ 为什么拆：选哪个 executor 需要知道能力目录和当前连通性（Nexus 有），而执行、处理失败、返回五态是纯执行（Scout 做）。所以 Nexus 算出**唯一一个** `executor_order` 塞进 `EXECUTE`，Scout 照单执行，失败不换路。

## 3. 内部分层

```
server_comm/transport/node.py  ← WS 客户端：REGISTER / HEARTBEAT / EXECUTE / RESULT
        │
        ▼
core.py  ScoutCore             ← execute() / observe() / manifest() / heartbeat()
        │                        纯本地、零网络
        ├─────────────► runtime/          准入 / 写边界 / 绑定 / 脚本运行时
        ├─────────────► router.py         唯一执行器分发（不选路）
        │                     │
        │                     ▼
        │              executors/         adb / ios_wda / playwright / mac·win 原生 + base
        │                     │
        │                     ▼
        │              platform/          各平台截图 / 层级 / 输入 / 脚本
        │
        ├─────────────► screen.py         按平台分派截图
        └─────────────► case_sources.py   用例数据源读取（xlsx / csv / API）
```

**`ScoutCore` 里禁止出现连接形态的分支**（`if 本地 / if 远程`）。core 只认 `ExecuteRequest → ExecuteResult`，transport 是外层的事。

## 4. 一次 `EXECUTE` 的完整路径

以 `tap_element`、`executor_order: ["adb"]` 为例：

```
1. server_comm/transport/node.py 收到 EXECUTE 帧
2. ScoutCore.execute：校验 execution_mode（agent / v5）
3. RequestGuard.begin：(run_id, step_idx) 是唯一键
   - 相同键不同内容 → request_key_conflict
   - 相同键仍在途   → request_inflight
   - 相同键已完成   → 直接回缓存 RESULT，不重新执行
4. BindingGuard：连接代际 / 进程代际 / 能力修订 / selected_impl 装配
5. 丢线程池执行，timeout_sec 是硬上限
6. router.dispatch：
   6.1 executor_order 必须恰好 1 个，且 selected_impl.executor 与之一致
   6.2 该 executor 必须在本机注册、supports 这条 cap、且服务该 sn 的平台
   6.3 执行一次，拿到五态（小写）
7. normalize_ui_result：写动作没有 execution 回执 → 收口为 blocked
8. 组装 RESULT：status / executor_used / attempts / elapsed_ms
9. transport 回 RESULT 帧（reply_to = 请求的 msg_id）
```

**V7 合同：任何状态都结束本次尝试。** Scout 不切换 executor、不改序、不追加通道；
`blocked` / `declined` / `fail` / `skipped` 都原样回报，由 Nexus 重新选路。
见 [CONVENTIONS.md](CONVENTIONS.md) §2。

## 5. 一次 `EXECUTE screenshot` 的路径

```
transport → ScoutCore.execute → screen.py
  按 device.platform 分派到该平台实现：
    android    → adb exec-out screencap -p
    ios        → WDA session screenshot
    web        → playwright_hub 的 page.screenshot()
    mac / win  → 桌面驱动截图
  空白帧检测（shot_is_blank）：判空则不当作有效帧
  → RESULT { image_base64, image_mime, width, height, source, data }
```

**不做多通道回退**：截图通道由设备平台唯一决定，`prefer` 之类优先序已删除。
**缩略图不在 Scout 侧做。** Scout 回原图，Nexus 负责压缩成 trace 用的 thumb ——
因为 thumb 只服务 UI，而 UI 是 Nexus 的事。

## 6. 远程通道（已取消）

`remote` executor 与 ClawNode 通道已于 2026-11 删除。现在 Scout 只有
adb / ios_wda / playwright 三种设备通道，加上 macOS / Windows 原生通道（`mac` / `win`），**不监听任何端口**，永远 dial 出去。

历史背景：上游 ClawNode 通过 WebSocket 反连执行端；本仓拆分后从未实现该通道，
只保留了“命令契约与 ClawNode 对齐”的 adb 内部实现（`platform/android/adb_command.py`）。
如果未来需要无 USB 的远程执行，应作为新通道重新立项，而不是恢复注释里的半成品。

## 7. 状态与生命周期

| 状态 | 存在哪 | 重启后 |
|---|---|---|
| 设备句柄 / 浏览器 lane | 进程内存（executor 与 playwright_hub 持有） | 重建 |
| 连通性探测结果 | 进程内存 + 短 TTL | 重探 |
| `(run_id, step_idx) → RESULT` 幂等记录 | 进程内存（`runtime/request_guard.py`） | 内存缓存丢失；持久 attempt、写意图及代际校验阻止跨重启自动重放 |
| 停止闩 `run_id → write_stopped` | 进程内存，取消/超时后单调保持 | 丢失；重启后按新进程代际处理 |
| 截图原图 | 临时文件，用后即删 | — |
| **任何业务数据** | **不存**，全在 Nexus | — |

**Scout 重启不会自动重放设备动作。** 已接单任务的持久记录用于恢复状态；遗留执行标为 interrupted，写入结果未知时保持资源隔离，等待停止和资源处置证据。

## 执行生命周期回归

代码片段保留调试上下文；`script_path` 整例执行从捕获版本加载原文及前后置，复用正式执行的断言/覆盖门禁。空 check 在两条路径都阻塞。用例后置失败仍尝试项目后置，`cleanup/error` 保留各清理失败，主失败优先。

`python -B scripts/check_case_lifecycle.py` 在临时源码与记录目录运行正式执行及真实调试队列，验证生命周期、覆盖、清理、回调分类、数据摘要和重复取码；CI 与打包前运行，不接真实设备或邮箱。取码会话只在内存保存已消费邮件的摘要，按邮箱/收件人隔离，不保存验证码。

邮箱取码先解析 HTML 正文，排除样式、脚本及附件；存在多个不同的候选数字时不猜测验证码。IMAP 每批最多读取 40 封尚未判定的邮件，后续轮询继续向前扫描，受既有截止时间约束。脚本动态邮箱取码必须先调用 `case.scout.otp_request(account)` 捕获 UIDVALIDITY/UIDNEXT，再发送验证码并用 `case.scout.otp(account, request=request)` 获取边界之后的新邮件。请求绑定当前 Case、账号、收件邮箱及策略，只能使用一次，最长 90 秒；整例重跑或调试 reset 清除请求，同收件人重新准备会使旧请求失效。邮箱代际变化及缺少请求均阻塞，不从用例起始时间回捞旧码。固定码兼容原接口。该边界排除准备前已收取的旧邮件；无法证明准备后延迟到达的邮件属于哪次业务发码，需要产品提供可匹配的请求标识才能保证这种关联。

### 正式服务入口切换

正式 Nexus 的 TLS 443 与 8443 是同一部署的入口别名。Scout 在 REGISTERED 确认相同 owner 后允许这两个入口复用原插件存储，保持已保存的服务归属字符串和凭据路径；执行记录继续使用首次已确认入口的 `/node` 路径计算目录。其他账号、主机、非 TLS 或其他端口仍隔离。入口切换使用 `mino-scout configure nexus-url` 并重启，不迁移账号归属，也不复制密钥。
