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
| `(run_id, step_idx) → RESULT` 幂等记录 | 进程内存（`runtime/request_guard.py`） | 丢失（Nexus 重发会重新执行，可接受） |
| 停止闩 `run_id → write_stopped` | 进程内存，取消/超时后单调保持 | 丢失；重启后按新进程代际处理 |
| 截图原图 | 临时文件，用后即删 | — |
| **任何业务数据** | **不存**，全在 Nexus | — |

**Scout 可以随时被 kill 并重启**，代价上限是"当前那一步重跑一次"。这是硬要求，不要引入需要优雅关闭才能保住的状态。
