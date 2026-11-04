# ENGINES — MinoScout 设备驱动层（现行）

## 1. 先纠正一个历史说法

上游 MiniOrangeServer 有一个 `driver/tentacle` 引擎层和 `EngineFactory`
（搬迁清单里的 **E3**）。**本仓从未搬迁它，现在也没有 `engines/` 目录或 `EngineFactory`。**

现行结构是：**executor 负责能力分派，`platform/<os>/` 负责该平台的原始动作。**
一个 executor 组合一个平台的一组函数，不再有“引擎对象”这一层。

| 平台目录 | 提供的原语 | 由谁使用 |
|---|---|---|
| `platform/android/` | `adb_command` / `adb_script` / `hierarchy` / `screen` / `low_level` / `script` | `adb` executor、脚本运行时 |
| `platform/ios/` | `wda_session`（纯 HTTP）/ `screen` / `script` | `ios_wda` executor、脚本运行时 |
| `platform/web/` | `playwright_hub` / `playwright_context` / `dom_hierarchy` / `screen` / `script` / `web_v5` | `playwright` executor、脚本运行时、V5 严格通道 |
| `platform/mac/`、`platform/win/` | `actions` / `focus` / `session` / `script` | macOS / Windows 原生 executor、脚本运行时 |

## 2. 为什么去掉引擎层

上游引擎对象同时承担三件事：建连接、缓存连接、把能力翻译成设备命令。
拆分后这三件事分别落到：

- 建连接 / 缓存 → executor 自己（adb 用 subprocess，Web 用 `playwright_hub` 的 lane，mac/win 用 Cua runtime）；
- 能力翻译 → executor 的 `execute()` + `low_level`；
- 连接形态选择 → Nexus 的 `executor_order`（唯一一个）。

多一层 `Engine` 只会让“谁持有连接”变成两处，和“Scout 可随时 kill”的约束冲突。

## 3. 多方案仍然必须可插拔

**硬规矩不变：不允许 `if backend == "appium"` 这类分叉。**
iOS 的 WDA 直连是当前唯一实现；如果以后加 Appium 或 simctl，做法是新增一个
executor / 平台模块，由 Nexus 选路，**不是**在 `WdaExecutor` 里加 `elif`。

判据：**加第二种后端时，如果需要修改现有类的方法体，说明抽象错了。**

## 4. ClawNode 通道已删除

`remote` executor、`clawnode/` WS 服务端、mDNS 发现与配对流程已于 2026-11 一并删除。
`platform/android/adb_command.py` 仍保留“与 ClawNode 命令同名同结构”的实现，
那只是 adb 通道的命令层命名兼容，**不是一条远程通道**。

Scout 因此不再监听任何端口：它永远 dial 出去（Nexus），设备侧只做主动连接
（adb / USB、WDA HTTP、Playwright 进程内、桌面 SDK 进程内）。

## 5. 生命周期与崩溃

| 事件 | 处理 |
|---|---|
| 设备掉线 | 下一次 probe 在 `HEARTBEAT.device_delta` 里如实报 `disconnected`；不自行反复重连 |
| 浏览器 / 桌面 runtime 卡死 | 超时后隔离 lane / 拒绝新输入，等下一次任务或重启进程 |
| Scout 进程重启 | 所有句柄重建。**这是可接受的** —— Scout 无持久状态，代价上限是当前那一步重跑 |

**不要为了保住连接而引入优雅关闭依赖。** Scout 必须能被随时 kill。
