# MIGRATION — 从 MiniOrangeServer 搬到这里

> 上游 `../MiniOrangeServer` **已停止维护，只读，禁止改动**。本仓是它的一半。
> 本清单由 `../MiniOrangeServer` 于 2026-09-02 的全量文件（392 个 `.py`）分类而来，**无待定项**。

> **2026-11 现状注记**：本文是搬迁当时的计划，不是现状。以下计划项没有落地，且已取消：
> `engines/` / `EngineFactory`（E3）、`probe/` 目录、`clawnode/` 与 `remote` 通道、
> `executors/low_level.py`、`executors/multi_tap.py`。现行结构见 [ENGINES.md](ENGINES.md)
> 与 [CLAUDE.md](../CLAUDE.md) §3；ClawNode/`remote` 通道已删除。

## 0. 总账

| 去向 | 文件 | 行数 |
|---|---|---|
| → MinoNexus | 186 | 60,165 |
| → MinoScout | 67 | 12,424 |
| 不搬（随上游归档） | 138 | 30,358 |
| 合计 | 392 | 102,947 |

不搬的 30,358 行是旧路径：`services/executor/`（旧 Plan 循环，`plan_execute_service` 零调用方）、`copilot_service.py`、`services/local/{locate,navigation,overlay,plan}`、`core/vision/`、`core/local_brain.py`、`services/shared/page_context/`、`feishu_regression_service.py`、`driver/` 的非 engine 部分。

**这批代码里含 `torch` / `open-clip-torch` / `paddleocr` / `paddlepaddle` / `ultralytics` / `opencv`。不搬 = 两个新仓库都不用养这些依赖。**

依据（实测于上游）：
- 批次执行路径运行时对 `local/locate`、`page_navigation`、`core/vision` **零调用**（`grep -rn` 在 `server/services/regression/` 下 0 命中）
- 定位走 `ai/regression/planner.py:398 locate_element()`，**纯 VLM**，不经 CLIP / OCR
- `services/executor/plan_execute_service` 无任何调用方

## 1. 搬迁时必须断开的四条边

上游的 live path 通过几处 lazy import 把上万行旧代码拖进依赖闭包。**搬迁不是复制粘贴，这四处要改写 import 指向。**

| # | 上游位置 | 实际用到的东西 | 不处理会拖进来 | 怎么改 |
|---|---|---|---|---|
| **E1** | `case_runner.py:66,419,719` → `app_automation_service` | 3 处 lazy import，含 `persist_run_finish`（落库） | `copilot_service` 4,411 行 → `clip_locate_service` → `clip_service`（**torch**） | Nexus 里独立成 `services/persistence/run_finish.py`，不带 copilot |
| **E2** | `case_precondition_service.py:355` → `page_navigation_service._screen_is_login_home` | **一个私有函数** | `page_navigation_service` 3,159 + `services/executor/execute_steps` 751 | 抽成独立小工具函数 |
| **E3** | `screen.py:381` + `remote_executor.py:138` → `driver/agent/Crawl/device_bootstrap.bootstrap_mobile_engine` | 2 个调用点，要一个 engine 对象 | 整个 `driver/tentacle` 引擎层 | Scout 里提炼成 `engines/factory.py::EngineFactory`。`driver/agent/Crawl` 这个位置在新仓库不存在 |
| **E4** | `case_precondition_service.py:295,356` + `app_automation_service.py:595` → `page_context_service` 的 `_collect_full_screen_text` / `_identify_page_by_screen_keywords`；`screen_frame_service.py:56` → `_shot_to_bgr` | 3 个私有函数 | `page_context_service` 804 行（连带 `core/vision`、`local/*`、`figma_*`） | 三个函数各自抽成独立小工具；`_shot_to_bgr` 直接内联 |

> E1 / E2 / E4 是**意外耦合**：几个私有工具函数和一个落库函数，合计拖进约 9,000 行和整个 torch 栈。E3 是真实功能依赖，需要提炼而非切断。

## 2. 搬到本仓（MinoScout）的文件

| 上游目录 | 文件 | 行数 | 文件名 |
|---|---|---|---|
| `driver/tentacle/engine/mobile/` | 12 | 4,947 | `__init__.py`, `adb_locator.py`, `appium_ios.py`, `ios_appium_runtime.py`, `ios_config.py`, `ios_locator.py`, `ios_runtime.py`, `mAdb.py`, `mIOS.py`, `mRemote.py`, `mobile_engine.py`, `wda_touch.py` |
| `server/services/regression/executors/` | 8 | 2,148 | `__init__.py`, `adb_executor.py`, `base.py`, `ios_wda_executor.py`, `low_level.py`, `multi_tap.py`, `playwright_executor.py`, `remote_executor.py` |
| `server/services/runtime/` | 11 | 1,477 | `adb_discovery.py`, `connectivity_probe.py`, `device_catalog.py`, `device_identity.py`, `ios_bonjour.py`, `ios_discovery.py`, `ios_ids.py`, `ios_simctl.py`, `ios_usbmux.py`, `ios_wda_session.py`, `playwright_hub.py` |
| `server/services/regression/` | 4 | 1,186 | `hierarchy.py`, `playwright_check.py`, `remote_engine_util.py`, `screen.py` |
| `driver/tentacle/engine/pc/` | 4 | 632 | `__init__.py`, `mMac.py`, `mWindows.py`, `mWindowsPywinauto.py` |
| `server/services/shared/` | 3 | 450 | `adb_script.py`, `clawnode_engine.py`, `clawnode_script.py` |
| `driver/tentacle/engine/vision/` | 5 | 368 | `__init__.py`, `mImageMatching.py`, `mOcr.py`, `mPositionCalculation.py`, `mSceneMatching.py` |
| `server/services/local/` | 1 | 297 | `adb_command.py` |
| `driver/tentacle/component/` | 3 | 275 | `__init__.py`, `map.py`, `router.py` |
| `driver/tentacle/core/` | 7 | 253 | `__init__.py`, `base_execption.py`, `base_logic.py`, `engine.py`, `exeception.py`, `memory.py`, `step_result.py` |
| `driver/tentacle/engine/web/` | 2 | 201 | `__init__.py`, `mChrome.py` |
| `server/services/shared/screenshot/` | 1 | 145 | `regression_capture.py` |
| `driver/tentacle/common/` | 3 | 45 | `__init__.py`, `mPath.py`, `platform.py` |
| `server/services/shared/device/` | 1 | 0 | `__init__.py` |
| `driver/tentacle/` | 1 | 0 | `__init__.py` |
| `driver/tentacle/engine/` | 1 | 0 | `__init__.py` |

### 落位映射

| 上游 | 本仓 |
|---|---|
| `server/services/regression/executors/*` | `mino_scout/executors/*`（只保留 adb / remote / ios_wda / playwright + base / low_level / multi_tap） |
| `server/services/regression/router.py` | `mino_scout/router.py`，**只保留 dispatch 半边**（≈150 行）。`_needs_locate` / `_inject_locate_coords` / `_ensure_screen_for_locate` 归 Nexus |
| `server/services/regression/screen.py` + `hierarchy.py` | `mino_scout/screen.py` + `hierarchy.py` |
| `server/services/runtime/{connectivity_probe,ios_*,adb_discovery,...}` | `mino_scout/probe/` + `mino_scout/engines/ios/` |
| `server/services/shared/{adb_script,clawnode_script,clawnode_engine}.py` | `mino_scout/clawnode/` + `mino_scout/executors/adb_script.py` |
| `server/services/shared/screenshot/regression_capture.py` | `mino_scout/screen_util.py`（`shot_is_blank` 等） |
| `server/services/local/adb_command.py` | `mino_scout/executors/adb_command.py` |
| `driver/tentacle/engine/**` + `component/{map,router}` + `common/` + `core/` | `mino_scout/engines/`（经 E3 提炼） |
| `server/websocket/device_manager.py` 的 **ClawNode 半边** | `mino_scout/clawnode/manager.py`（见下） |

### `device_manager.py` 要劈成两半

上游 `server/websocket/device_manager.py`（1,623 行）同时干三件事，拆分后归属不同：

| 职责 | 去哪 |
|---|---|
| UI observers 广播（`broadcast_to_observers`） | Nexus |
| Scout 节点登记与路由 | Nexus（新写） |
| **ClawNode 连接管理、配对、`send_command`、capability manifest 摄取** | **Scout** |
| `send_command` 里"目标是 adb 直连设备则走 `run_adb_command`"的通道分叉 | Scout（并入 `router.py`） |

这是全仓最大的一处需要**人工劈开**的文件，不能整体搬。

### 不要搬进来的

`server/services/shared/page_context/`、`screen_frame_service.py`、`server/services/local/{locate,navigation,overlay,plan}`、`core/vision/`、`driver/agent/`（除 E3 提炼的部分）、`driver/tentacle/component/` 里除 `map.py` / `router.py` 之外的组件 DSL。

## 3. 归 MinoNexus 的部分

见 `../MinoNexus/docs/MIGRATION.md`。两仓的清单互补，合起来覆盖上游全部 392 个文件。

## 6. 搬迁过程中对 §2 分类表的修正

分类表是静态规则跑出来的，实际搬的时候发现两处归错了。**以本节为准。**

| 文件 | 原判 | 实际 | 依据 |
|---|---|---|---|
| `server/services/runtime/app_query.py`（113 行） | → Nexus | **→ Scout** | 它是纯字符串解析（`parse_package_version` / `parse_foreground` / `FOREGROUND_SHELL`），`adb_executor` 的 `get_app_version` / `get_foreground_app` 直接依赖。零依赖、零 stdlib 之外的东西。落位 `mino_scout/probe/app_query.py` |
| `server/services/shared/adb_script.py` + `server/services/local/adb_command.py` | → Scout（已判对） | → Scout | 但它们互相 import，必须一起搬，且 `adb_command` 里的 `_download_apk` 是 `install_apk` 的 url 路径依赖 |

另外确认了一件好事：**adb 通道的依赖闭环非常干净**。`hierarchy.py`(428) / `app_query.py`(113) /
`multi_tap.py`(29) / `adb_command.py`(297) / `adb_script.py`(228) 这 1,095 行里，跨包引用**只有
`script.log.SLog` 一处**；`app_query.py` 与 `multi_tap.py` 甚至是逐字节原样搬。

## 7. 已搬迁清单（持续更新）

| 本仓路径 | 上游来源 | 行 | 改动 |
|---|---|---|---|
| `mino_scout/log.py` | `script/log.py` | 115 | 去掉 DB 回调；加 run_id 前缀与 `redact()` |
| `mino_scout/protocol.py` | 新写（协议） | 303 | — |
| `mino_scout/schemas.py` | `ai/regression/schemas.py` | 112 | 只取 PlanEvent / EventResult / CapturedScreen；新增 5 个 Nexus 下发字段 |
| `mino_scout/executors/base.py` | `executors/base.py` | 152 | `RunContext`→`DeviceRef`；`supports()` 加 `low_level` 参数 |
| `mino_scout/executors/low_level.py` | `executors/low_level.py` | 313 | **只改 1 行 import** |
| `mino_scout/executors/multi_tap.py` | `executors/multi_tap.py` | 29 | **逐字节相同** |
| `mino_scout/probe/app_query.py` | `runtime/app_query.py` | 113 | **逐字节相同** |
| `mino_scout/hierarchy.py` | `regression/hierarchy.py` | 428 | 只改 1 行 import |
| `mino_scout/executors/adb_command.py` | `local/adb_command.py` | 297 | 只改 2 行 import |
| `mino_scout/executors/adb_script.py` | `shared/adb_script.py` | 228 | 只改 2 行 import |
| `mino_scout/executors/adb_executor.py` | `executors/adb_executor.py` | ~620 | **10 处语义适配**，见下 |
| `mino_scout/router.py` | `regression/router.py` 的 dispatch 半边 | 200 | 重写：不做选路，只按 `executor_order` 试 |
| `mino_scout/screen.py` | `regression/screen.py` 的 adb 通道 | 155 | 只搬 adb；remote/ios_wda/playwright 待 E3 |

### `adb_executor.py` 的 10 处语义适配

1. `_declares_adb_low_level(cap)` 回查能力目录 → `_runnable_low_level(low_level)` 看 Nexus 传进来的声明
2. `supports(cap)` → `supports(cap, low_level)`（Protocol 同步改了，见 `base.py`）
3. `ctx.run_context.adb["serial"]` → `ctx.device.adb_serial`
4. `ctx.run_context.sn` → `ctx.device.sn`
5. serial 缺失的报错文案改成指向 `EXECUTE.device_hint`（真正的原因）
6-7. `getattr(ctx.run_context, "target_package")` → `ctx.device.extra["target_package"]`（2 处）
8. 去掉 `stamp_app_version(ctx, name)` —— 写回 RunContext 是 Nexus 的事，版本放 `raw_response`
9. `low_level` 优先从 `event.low_level` 取，`ctx.selected_impl` 兜底
10. 其余全是 import 路径

## 7.1 第二批（transport + playwright）

| 本仓路径 | 上游来源 | 行 | 改动 |
|---|---|---|---|
| `mino_scout/playwright_hub.py` | `runtime/playwright_hub.py` | 278 | 只改 1 行 import；**新增导出 `PROBE_OK_STATE`** |
| `mino_scout/executors/playwright_executor.py` | `executors/playwright_executor.py` | 313 | 3 处 `run_context`→`ctx.device`；`supports()` 加 `low_level` 参数；新增 `provides` + `probe()` |
| `mino_scout/screen.py` 的 playwright 通道 | `screen.py::_capture_via_playwright` + `_compress_web_png` | +80 | 压缩比改为参数（见下） |
| `mino_scout/core.py` | 新写 | 300 | ScoutCore：execute / observe / manifest / 幂等 / cancel |
| `mino_scout/transport/node.py` | 新写 | 300 | 九条消息的 WS 客户端 |
| `mino_scout/cli.py` | 新写 | 100 | `mino-scout run` / `mino-scout probe` |

### 协议改动：`OBSERVE.compress_ratio`（v1 加字段）

上游 Web 截图的压缩比来自 `system_settings_service.get_ai_web_compress_ratio(provider_id)`
—— **按 LLM provider 取值的 Nexus 设置**。Scout 不读设置，所以改为随 `OBSERVE` 下发，
默认 `2.0`（与上游 fallback 一致）。已走完协议四步流程，两仓 fixture 哈希一致。

`RESULT.width/height` 仍报**原图**尺寸 —— 千分比坐标体系不受压缩影响。

### 搬迁中修掉的三个真 bug

| # | 问题 | 怎么发现的 |
|---|---|---|
| 1 | `probe_playwright()` 的成功态是 `"available"` 而不是 `"connected"`，我第一版按 `connected` 比较 → playwright 永远上报不可用 | 跑 `manifest()` 时发现 Chromium 明明装了却报 unavailable。已导出 `PROBE_OK_STATE` 常量，避免第三处再写错 |
| 2 | `_on_frame` 里 `asyncio.create_task()` 没持引用 → 任务被 GC，请求静默消失 | e2e 测试里 OBSERVE 没有任何回应、也没有任何日志 |
| 3 | **`_connect_once` 里先 `_register()` 再起接收循环** → REGISTER 等的应答只能由接收循环派发，于是死等超时、一条帧都处理不到 | 同上。这是 e2e 测试存在的意义 |

第 3 条尤其值得记：单测和假 transport 都发现不了它，只有真的开一个 WebSocket 才会暴露。

## 8. 尚未搬迁（下一批）

| 待搬 | 行 | 卡在哪 |
|---|---|---|
| `screen.py` 的 remote / ios_wda 两通道 | ~340 | 依赖 `EngineFactory`（E3）。**playwright 通道已搬** |
| `remote_executor.py` | 593 | 依赖 `EngineFactory` |
| `ios_wda_executor.py` | 153 | 依赖 `ios_wda_session` + `EngineFactory` |
| `EngineFactory`（`driver/tentacle/engine/**`） | ~6,000 | **E3，唯一需要真设计的部分** |
| `probe/`（连通性探测） | ~1,300 | — |
| `clawnode/`（含劈开 `device_manager.py`） | ~800 | 需与 `../ClawNode` 协同 |
| ~~`core.py` / `transport/node.py`~~ | — | **已完成**，见 §7.1 |

## 4. 已知行为变化

| # | 变化 | 影响 |
|---|---|---|
| 1 | **ClawNode 的连接对象从 server 改为 Scout** | 配对配置（`ws_url` / `auth_token` / `gateway_id`）要指向 Scout。**需要与 `../ClawNode` 仓库协同改动**，不是任一新仓库能单方面完成的 |
| 2 | 单机从一个进程变成两个进程 | `../MiniOrange`（Electron）的 `electron/main.js` 要 spawn 两个二进制，并把两个都纳入现有 `taskkill` / `pkill` 清理逻辑（上游 `main.js:69-91`）。UI 端口仍是 `10104`，`vite.config.js:9` 不用改 |
| 3 | Scout 起不来时 | Nexus 必须能独立启动，并在 UI 上明示"无可用执行节点"，而不是整个后端起不来 |
| 4 | `sn` 之外新增 `node_id` | 设备表要加 `node_id` 归属，否则 Nexus 答不出"这台设备派给哪个节点"。见 Nexus 的 `docs/NODE_REGISTRY.md` |
| 5 | 不再有进程内直调 | 上游 `driver/agent/in_process_server_query.py` 那套 `builtins.SERVER_QUERY` 注入不再需要，Scout 通过协议拿一切 |

## 5. 验收

搬迁完成的判据不是"编译通过"，而是**行为对齐**：

1. 在上游 MiniOrangeServer 上跑一条批次用例，存档时间线、每步截图、覆盖度结论、总耗时
2. 在 Nexus + Scout 双进程上跑同一条用例
3. 逐项对照：步骤数、每步 capability 与 executor、断言结论、覆盖度码、UI 表现

**耗时允许劣化（多了两次 WS 往返），结论不允许变。**
