# EXECUTORS — MinoScout

本仓有三种设备通道：`adb`（Android）、`ios_wda`（iOS）、`playwright`（Web），
外加**原生通道**：macOS 上是 `mac`，Windows 上是 `win`。另有一个宿主插件面
`host_plugin`（仅声明 `plugin.gmail.fetch_otp`）。它们共同点是：**直接操作设备**。
上游另外四个（`hitl` / `vlm` / `ai_persona` / `internal`）零设备访问，归 MinoNexus。

`remote`（ClawNode）通道已于 2026-11 删除；Scout 不再有第五种设备通道。

## 1. 能力矩阵

`provides` 是具体能力 id（与 Nexus 能力目录同一词表）。Scout 在 `REGISTER` / `HEARTBEAT`
里上报这些字符串，Nexus 据此选路。

| capability | adb | ios_wda | playwright | mac | win |
|---|:--:|:--:|:--:|:--:|:--:|
| `launch_app` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `close_app` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `kill_app` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `tap_element` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `tap_xy` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `long_press_element` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `input_text` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `press_key` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `swipe_direction` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `swipe_element_to_element` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `set_clipboard` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `get_foreground_app` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `screenshot` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `hierarchy` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `exec_script` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `install` | ✅ | ✅ | — | ✅ | ✅ |
| `uninstall_app` | ✅ | ✅ | — | — | — |
| `get_app_version` | ✅ | ✅ | — | — | — |
| `read_device_data` | ✅ | — | — | — | — |
| `set_input_method` | ✅ | — | — | — | — |
| `clear_app_cache` | ✅ | ⚠️ | — | — | — |
| `probe_device_state` | ✅ | — | — | — | — |
| `wake_screen` / `dismiss_keyguard` | ✅（low_level） | — | — | — | — |
| `read_web_auth` | — | — | ✅ | — | — |
| `wait_screen_ready` | — | — | ✅ | — | — |
| `switch_tab` | — | — | ✅ | — | — |
| `open_tab` | — | — | ✅ | — | — |
| `upload_file` | — | — | ✅ | — | — |
| `reload_page` | — | — | ✅ | — | — |

平台：`adb` → Android；`ios_wda` → iOS 真机 / 模拟器；`playwright` → Web；`mac` → macOS；`win` → Windows。
`DeviceRef.compatible_executors` 是这条映射的唯一真源（不在文档里另写一份）。

> 上面两列 `mac` / `win` 既是平台名，也是 `REGISTER` 里的 executor id。`desktop`
> 只作为历史平台别名和旧版载荷的入站兼容值存在，不再出现在新报文里。

⚠️ = 收到时返回 `declined`（让位），不是 `fail` —— 否则“本来就做不到”会被计入失败率。

## 2. Executor 接口

```python
class Executor(Protocol):
    id: str   # "adb" | "ios_wda" | "playwright" | "mac" | "win"

    def supports(self, capability_id: str, low_level: dict | None = None) -> bool:
        """Router 的硬校验。便宜、纯判断、不碰设备。"""

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        """跑这条动作。必须捕获全部异常，返回五态之一，永不抛。"""
```

五态语义见 [CONVENTIONS.md](CONVENTIONS.md) §2 —— **任何状态都结束本次尝试，Scout 不换路。**

`ExecutorContext` 携带：`device`（来自 `EXECUTE.device_hint` / `sn`）、`run_id` / `step_idx`、
`selected_impl`（Nexus 从能力目录抄来的 impl 元数据）、`shared`（同一次派发内的
`write_stopped` 回调）。没有屏幕帧、没有抓图优先序 —— 那些字段已删除。

## 3. `low_level`：不写 Python 就能加能力

`EXECUTE` 载荷里的 `low_level` 段直接抄自 Nexus 能力目录 YAML：

```json
{"low_level": {"command": "TAP", "params": {"x": "{x}", "y": "{y}"}}}
{"low_level": {"shell": "input tap {x} {y}"}}
```

`platform/android/low_level.py` 负责：占位符 `{x}` 用 `params` 填充 → 按 `shell` / `command`
分派到 adb → 组装五态结果。**low_level 只在 adb 通道生效。**

**因此绝大多数新能力在 Scout 侧零改动**：Nexus 加一条 capability YAML + 一条 `implementations`，
Scout 自动能执行。只有引入**新原语**（现有 `command` / `shell` 表达不了的动作）时才需要动 Python。

## 4. 加一个 executor

1. **Nexus 侧**（不在本仓）：能力目录声明该 executor 的 `provides`，相关 capability 补 `implementations`
2. 本仓新增 `executors/<id>_executor.py`：实现 `supports()` + `execute()` + `probe()`
3. `cli.build_core()` 里装配
4. `DeviceRef.compatible_executors` 里登记它能服务的平台
5. 本文件矩阵补一列
6. `mino-scout probe` 确认它被上报为 `available`

**注意第 1 步在另一个仓库。** 只在 Scout 加 executor 不会被用到 —— Nexus 不知道它存在，就不会把它放进 `executor_order`。

## 5. 加一种“新原语”

如果新能力需要现有 `low_level` 表达不了的动作（例如“录屏 10 秒”）：

1. 先问：能不能拆成已有原语的组合？能就在 Nexus 侧用 `expands_to_events` 表达，本仓零改动
2. 不能，才在对应 executor 里加一个 `command` 分支，并在本文件 §1 矩阵登记
3. 新能力必须同时在 Nexus 的能力目录登记，否则 `REGISTER` 上报的字符串在 Nexus 侧无处匹配

## 6. 反模式

| 不要 | 为什么 |
|---|---|
| 在 executor 里判断"这一步该不该做" | 决策在 Nexus。Scout 只执行 |
| 在 executor 里调 LLM 兜底 | 违反 CLAUDE.md §1 约束 2（脚本运行时的本地模型调用是单独合同，不在这里） |
| 在 executor 里自行更换通道 | 通道由唯一的 `executor_order` 决定；Scout 不换路 |
| 条件不满足时返回 `fail` | 应返回 `declined`，否则失败统计被污染 |
| 为一个能力写 `if capability_id == "xxx"` 长链 | 用 `low_level`。上游 `adb_executor.py` 的 if-chain 是被明确批评过的反模式 |
| 省略 `executor_used` | Nexus 的 trace 与覆盖度依赖它 |


## macOS / Windows 原生执行器（V4）

原生通道只处理 `platform=mac` / `platform=win`，不在 Android/Web 执行器中 fallback。支持 `screenshot/tap_xy/tap_element/input_text/press_key/launch_app/get_foreground_app`，其中截图复用 ScreenCapture 的 Desktop 分支。Cua Driver 0.34.1 的 `list_apps` 不返回应用版本，SDK/原生工具也未提供 `get_app_version`；不把 `driver_version` 当成应用版本，Nexus 按设备上报能力跳过版本采集。Cua Driver 0.34.1 是可选 native 依赖；manifest 消费 Cua `health_report` 的版本、会话、权限和 AX 状态，实际窗口捕获还须取得有效捕获引用。

Desktop 首版只接受 visual 流程。截图坐标按 Mino 的千分比转换成当前窗口图像像素，再带 capture ID 和 `ActionTarget.WINDOW(pid, window_id)` 交给 SDK；窗口到屏幕及 Retina/缩图坐标映射由 Cua 处理。输入与按键也需要本任务的新截图、未变化的会话代际和独占租约；精确窗口的前台激活、投递及校验由 Cua `delivery_mode=foreground` 负责。macOS 启动使用已配置 bundle ID，Windows 使用完整 EXE 路径或 AUMID，经结构化 launch 调用确认目标进程并绑定窗口，不要求启动后保持前台，不执行 shell。组合键和 modifiers 按目标 OS 规范化。动作证据保留 Cua effect/route/delivery/error.code；拒绝为 blocked，疑似无效果或部分执行为 fail，不标成功；无业务回读的动作仍需后续截图确认。

能力实现配置由 Nexus 的目录决定；Scout 不读取 YAML，也不让“在线”替代 readiness。

## Desktop 输入契约（0.1.76）

自动选窗只接受当前可见、未最小化、宽高均至少 64 点的窗口；不把隐藏窗口或工具条当作应用内容窗口。显式指定 `window_id` 时仍按该 ID 绑定，不自动换窗。启动诊断增加 `window_bounds`，便于确认实际绑定的范围。

Nexus 将 Desktop 渠道映射为 `desktop_visual`，PC 输入只派给 `desktop`，不进入 Android / ADB 前台恢复。支持 `current_focus` 下的 `replace` 与 `append`；成功点击后由 Nexus 提供 `focus:<run_id>:<seq>`，Scout 只保存字段落点、窗口目标和窗口尺寸。输入前以新窗口截图的 capture ID 在 Cua 中重新点击该字段；窗口或尺寸变化、代际变化、取消、输入后或超时使引用失效。`replace` 经 Cua 发送 macOS Command+A / Windows Ctrl+A；`append` 发送 Command+Down / Ctrl+End 定位末尾，再输入正文。全部输入绑定相同窗口与 foreground 模式，不自行读取原生键盘焦点。

Windows 被测应用标识为完整 EXE 路径或 AUMID，macOS 为 bundle ID；不执行任意启动命令。Android 清缓存、force-stop、reset 等恢复动作不用于 Desktop。

Session Log 的 `tool/result` 包含节点、实际 executor、尝试链、capture_id、session_generation、驱动 effect/route 和安全的原生 evidence 类型；恢复子动作同样记录执行诊断。`observe/screen` 保留捕获引用，Workbench 可关联前后图和请求落点。自由文本 evidence detail 不展示，避免泄露输入内容。驱动局部 verify_state 未采集时明确标注，不能把动作投递成功等同于业务断言通过。

### 2026-10-12 看图模式兼容性修正

macOS 和 Windows 启动均通过 Cua `launch_app` 确认正整数 pid，再有界等待目标进程的普通窗口并绑定 `pid + window_id`；启动调用遵循已锁定 SDK 的工具 schema，不附加不支持的 session。启动准入不调用 `bring_to_front`，不要求 `activated=true` 或全局前台归属目标应用。仅请求受理且缺少有效 pid、目标窗口不存在或无法唯一选窗时仍阻断；启动成功不代表窗口截图、输入或业务断言已成功。

前台身份只消费 Cua `list_apps.active` 和配置的应用标识，不再调用自研 NSWorkspace/AX/Win32 探针。前台结果不能唯一确认或缺少应用身份时阻塞；不会把未知状态当作应用已离开前台。健康检查统一调用 Cua `health_report`，跳过 `screen_capture_capability`，避免健康探测顺带抓整屏或申请权限。

启动执行 Cua launch_app（已绑定且进程仍在则复用）→ 等待有效目标窗口 → 保存运行窗口绑定。窗口列表非空但没有可绑定普通窗口时继续有界等待，不重复启动。launch 诊断保留 stage、launched、activated、window_verified、foreground_verified 和目标 pid/window_id；成功 stage 为 window_ready，window_verified=true 表示窗口身份已确认，foreground_source=not_required、activated=false、foreground_verified=false 表示没有请求或验证持续前台激活，不表示启动失败。多个窗口优先选择当前可见窗口，并仅在完整 z_index 可唯一确定最前窗口时自动选择；否则要求明确 window_id，不随意选择列表首项。

点击的 typed `ActionResult` 与输入/按键的 `ToolResult.action`、JSON 回执分别解析。`unverifiable` 表示动作已投递但效果待回看，不视作已完成业务，也不当作投递失败；额外返回 `verification_required=true`。输入摘要明确标记“已写入当前焦点”，与 Nexus 的输入投递契约一致，业务结论由下一张截图确认。拒绝、部分执行、疑似无效果仍保留各自的驱动证据。启动失败的诊断保留 stage、pid、窗口与前台标识。

Desktop 图像的 `compress_ratio` 为 1–10，默认 1。通过 SDK 通用 `get_window_state(pid, window_id, include_accessibility_tree=false, include_screenshot=true, display_only=false)` 获取注册窗口捕获，保留 typed 输出未暴露的 capture_id。比值大于 1 时先校准原尺寸，再由 Cua `max_image_dimension` 生成并注册最终缩图；Scout 不自行缩放、裁切或重编码图像。最终图像尺寸等于 capture_width/capture_height，原尺寸记录在 native_width/native_height。截图和动作诊断保留 capture_scope=window、target、pid/window_id、capture_id、snapshot_id 和 session_generation。新截图失败会使旧观察引用失效，窗口丢失、所有者改变或捕获拒绝不回退整屏。

窗口截图不要求被测应用此刻占据全局前台。get_foreground_app 继续返回真实诊断；该结果不能推翻已确认的进程和窗口绑定，Nexus 的全局前台自动恢复也不用于 Desktop。后续输入的窗口存在、所有者、捕获几何与前台投递仍由 SDK 独立校验。此项不承诺所有应用均支持完全后台输入：点击、文本和按键继续使用 delivery_mode=foreground，让 Cua 在每个动作中处理所需激活和恢复；截图失败或 SDK 拒绝投递保留真实失败，不回退整屏、不改为成功。


### 2026-10-13 批量取证路由与报告跳转

ADB 声明的 supports 与实际 low_level 解释器保持一致：shell 使用非空 shell；shell_batch 使用包含 name/shell 的非空 commands；shell_seq 使用非空 steps。未知 kind 或无效结构不冒充支持。probe_device_state 等目录声明的批量能力可走此通路，不增加硬编码能力名单。

飞书原生结果卡片将 actions 中的有效 HTTP/HTTPS URL 转成报告跳转按钮。按钮只打开页面，不回调执行请求；流式草稿不附加报告按钮。文本降级仍由 Nexus 提供完整 URL。协议字段没有变化。


### 2026-10-16 有限宿主插件执行面

除设备执行器外，`host_plugin` manifest 为既有插件处理器声明唯一具名读取 `plugin.gmail.fetch_otp`。它不属于设备 UI 通道，不进入设备 fallback，不改变设备 provides 矩阵。插件账号归属核验前 unavailable；configured 由独立插件状态与 Nexus 依赖合同核对。绑定请求必须使用完整 v1 投影，不能以空执行器或其他设备执行器取码。其他插件仍沿用原合同，不自动取得绑定资格。

## V7 路由约束（2026-10-20）

普通设备动作只执行 EXECUTE 指定的一个 executor，selected_impl.executor 必须一致。任何执行状态都不尝试后备执行器；保留实际 executor_used、attempts 和 execution 写边界证据。旧版依次 fallback 的说明不再适用。

## 7. 原生通道命名

平台语言只有 `mac` / `win`，executor id 与平台同名。`desktop` 不再作为 executor id 出现。

两仓已在同一周期切换：

| 侧 | 落点 |
|---|---|
| Scout | `executors` 注册键、`DeviceManifest.channels`、`runtime_input_capabilities.executor`、`compatible_executors`、`platform_contract` / `input_contract` 校验、`script_exec` |
| Nexus | `executors_for_platform`、`executor_channels`、`device_store._kind_of`、`desktop_catalog` 的 `executor`、`core/platform_contract`、`loop/input_contract`、`run_context` 的通道读取、`page_ready` |
| 兼容 | 入站 `executor_order=["desktop"]` / 旧 `channels.desktop` / 旧 `platform=desktop` 仍按宿主平台解析；出站只发 `mac` / `win` |

升级顺序：Nexus 与 Scout 同周期发布。切换期新 Nexus 对只上报 `desktop` 的旧 Scout
保留一条只读回退（`executors_for_platform` 的 `mac/win → 同平台 + desktop`、能力目录按
节点实际可用 id 改写），旧 Nexus 则认不出 `mac` / `win`，所以 Nexus 必须先升级。
