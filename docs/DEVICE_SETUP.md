# DEVICE_SETUP — MinoScout

一台机器要成为可用的 Scout 节点，需要哪些环境。Scout 启动时会探一遍，探不到的通道在 `REGISTER` 里报 `available: false` + `reason`。

多机账号兼容：首次通过 Nexus 确认插件账号归属后，新插件使用按服务地址和账号隔离的存储空间；配置、安装目录、IM 库与系统保险库条目均独立。原电脑无归属的旧文件和密钥保留原位，不会被认领、读取或自动删除，也不会阻塞新插件配置。已确认归属的旧配置继续使用原路径；更换账号或服务仍拒绝继承，不支持静默转移。节点设备区仅展示当前节点连接的设备；设备历史仍保留。

## 1. 通道总览

| 通道 | 必需外部依赖 | 探测方式 | 探不到的后果 |
|---|---|---|---|
| `adb` | `adb` 在 PATH；设备开 USB 调试并已授权 | `adb -s <sn> shell echo ok` 含 `ok` | 该 Android 设备不可用（没有备用通道） |
| `ios_wda` | usbmuxd；WDA 已装并可拉起；真机需签名 | 能建 WDA session | iOS 设备不可控 |
| `playwright` | `playwright install chromium` 已执行 | `playwright_hub` 能拉起 Chromium | Web 用例不可跑 |
| `mac` / `win` | macOS/Windows 交互桌面 + Cua Driver 0.34.1 + 屏幕录制/辅助功能权限 | 原生 runtime `state=ready` | 该宿主不能跑 PC 用例 |

`remote`（ClawNode）通道已于 2026-11 删除，不再有“没有 USB 调试就用远程”的备选路径。

## 2. adb

```bash
adb devices -l        # 应看到设备且状态为 device，不是 unauthorized / offline
```

| 常见问题 | 处理 |
|---|---|
| `unauthorized` | 手机上确认 RSA 指纹弹窗；`REGISTER` 里该设备 `channels.adb = "unauthorized"` |
| 多台机器抢同一台设备 | adb server 是单机独占的。**同一台设备不要挂在两个 Scout 节点上** —— Nexus 的 `node_id` 归属唯一，重复挂载行为未定义 |
| TCP adb 断连 | Scout 会在 `HEARTBEAT.device_delta` 里报 `disconnected`，不自行反复重连 |

## 3. iOS（`ios_wda` 通道）

| 项 | 要求 |
|---|---|
| 真机 | usbmuxd 可用；WDA 已用有效签名安装；设备已信任本机 |
| 模拟器 | `simctl` 可用；UDID 形态与真机不同，由 `ios_ids` 区分 |
| Appium | 当前不走。WDA 直连是唯一实现；新增后端必须新加 executor，不在 `WdaExecutor` 里分叉（见 [ENGINES.md](ENGINES.md) §3） |
| WDA 挂了 | Scout 发 `EXECUTE {capability_id: node.engine_crashed}`，下次 `get()` 重建 |

`../WebDriverAgent` 是本地的 WDA 工程副本，可用于重新签名与安装。

## 4. Playwright（Web 通道）

```bash
playwright install chromium
```

- Chromium 由 `playwright_hub` 在 Scout **进程内**拉起，和 adb 平级
- 设备列表里的 `web` + `scout_id`（如 `web3f8a1c0e9b2d4f71`）是**虚拟槽**，不是真实设备行
- Chrome 走 CDP、Firefox 走 BiDi 都由 Playwright 消化。**不要另做 CDP / BiDi 驱动**
- Web 场景优先按按钮 / 链接名字点，坐标兜底 —— 因此 `playwright` 的实现 `needs_vlm: false`，比 VLM 路径 cost 更低

## 5. 网络

| 项 | 规定 |
|---|---|
| Scout → Nexus | 出向 WS。默认 `ws://mino.local:10104/node`（Nexus 启动时注册该名） |
| Scout 监听 | 无。Scout 只 dial 出去，不监听端口 |
| 系统代理 | 必须绕过 `mino.local` 与 loopback。本仓 `configure_proxy_bypass` 写入 `no_proxy` |

## 6. 自检

```bash
mino-scout probe          # 只探测并打印 manifest，不连 Nexus
```

输出即 `REGISTER` 里将要上报的 `executors[]` + `devices[]`。**部署一台新节点时先跑这个**，确认矩阵符合预期再接入 Nexus。


## V4 PC 桌面通道

每台宿主登记一个 `desktop:<node_id>:console`，与 `web<node_id>` 和手机设备并存。Scout 重启会更新 `session_generation`，不会增加设备。窗口/显示器不创建额外目标。

| 宿主 | 当前执行范围 | 未就绪时 |
|---|---|---|
| macOS arm64/x86_64 | 可选 Cua Driver 0.34.1 适配，visual 截图/捕获坐标点击/输入/按键/启动及聚焦目标应用；实际硬件与权限仍需验收 | 分别报告无会话、锁屏、权限不足、插件缺失/版本不符或 native 探测失败 |
| Windows x64/arm64 | Cua Driver 0.34.1 visual 截图/捕获坐标点击/输入/按键/启动及聚焦目标应用；发行包目前为 x64，arm64 需源码安装；真实 Windows 验收仍待完成 | 无交互登录、RDP 断开、Session 0、锁屏、SDK 缺失/不兼容和 native 探测失败均禁止输入 |
| Linux | 宿主与图形会话环境可见；原生 Desktop 执行未开放 | `no_session` 或 `unsupported` |

源码安装按需要使用 `pip install ".[desktop]"`，只接受锁定的 SDK 版本。macOS/Windows 发布流水线包含该 extra，并在冻结前检查原生库可导入；冻结包同时收集 SDK 原生库及 distribution metadata；运行层 ABI 升级到 4，旧运行层不可冒充已具备 Desktop。构建配置不等于实际发布/安装验收。

macOS 必须在已登录的交互会话内启动，并给实际运行 Scout 的程序授予屏幕录制和辅助功能权限。可在 Workbench 的节点页展开“处理权限”；按系统提示重启程序。探测只读，不代用户授权或解锁。仅安装 Scout/SDK 不会使桌面自动变成可选状态。

Windows 计划任务使用安装用户的 `Interactive` 登录态和 `Limited` 权限，在该用户登录时启动；不再用 SYSTEM/Session 0 操作桌面。重跑安装会更新同名任务；原有 SYSTEM 部署须在实际登录用户内重新运行安装。RDP 会话必须保持连接。探测读取输入桌面名称，不切换桌面，不操作锁屏或 UAC 安全桌面；权限边界不足时由驱动拒绝，不能默认提升权限。

任务应用标识：macOS 使用 bundle ID；Windows 使用完整 EXE 路径或打包应用 AUMID（例如 `Microsoft.WindowsNotepad_8wekyb3d8bbwe!App`），不使用模糊进程名。启动、聚焦后回读前台身份；输入前再次确认任务目标，不能切换到另一个应用。按键支持 `key` 与字符串列表 `modifiers`，或 `Ctrl+C` 形式；`primary` 在 macOS 映射 Command、Windows 映射 Ctrl。

桌面操作共用一个持久 SDK、事件循环和串行线程；截图带 capture ID，动作尝试消费截图引用。输入要求服务端任务租约及相同会话代际。Windows 会话身份包含登录 AuthenticationId，锁屏/注销/会话切换都会使旧引用失效。超时结果未知时停止接纳该 native runtime 的新操作，查看真实状态并重启 Scout；不自动重试、不创建另一条 native lane。取消立即使引用失效并串行结束 SDK 会话；进程退出关闭 SDK。

当前完成的是协议、HTTP/WebSocket 与 SDK 替身的隔离联调，尚未验收真实原生应用截图、点击、输入和跨 OS 部署，不能标为全部 PC 已支持。

### 在 Workbench 启动 PC 应用测试

1. 在 Scout 所在电脑登录并解锁桌面，确认被测应用已经安装。执行 `mino-scout status` 检查 Nexus 连接和注册，再执行 `mino-scout probe`。PC 设备必须同时显示 `desktop_state=ready` 和 `channels.desktop=connected`；节点在线或进程存活不足以开始桌面测试。
2. macOS 若显示 `permission_required`，在系统设置中给实际运行 Scout 的程序授权屏幕录制和辅助功能，再按系统提示重启。`driver_missing` / `driver_incompatible` 表示需要包含匹配 Cua Driver 0.34.1 的发行包；源码开发使用 `pip install ".[desktop]"`。Windows 在测试用户的已登录交互桌面运行，RDP 保持连接。
3. 在 Workbench 打开应用的配置页，添加 macOS 或 Windows 被测应用渠道，填写“启动标识”并保存。macOS 填 bundle ID，例如 `com.apple.TextEdit`；可用 `defaults read /System/Applications/TextEdit.app/Contents/Info CFBundleIdentifier` 查询，第三方应用替换为其实际 `.app` 路径。Windows 填实际完整 EXE 路径，例如 `C:\Windows\System32\notepad.exe`，或已安装打包应用的 AUMID；可用 PowerShell `Get-StartApps` 查找。显示名称、`.app` 路径和模糊进程名不能替代 macOS bundle ID。
4. 先建一条冒烟用例：前置条件为应用已安装且桌面已解锁；步骤写“启动并聚焦被测应用，确认主窗口出现，点击输入框并输入测试文本，再核对显示内容”。测试应用的空白文档可用作输入目标，避免首次就在真实业务数据上操作。Desktop 的 `launch_app` 由 Scout 调用原生驱动启动和聚焦，并回读前台身份；PC 不使用 Android 的清缓存或 force-stop。
5. 在用例运行栏或“新建任务”中，选择对应被测应用渠道和运行环境，执行方式选择“看图 visual”，设备选择该节点的“PC 桌面”，勾选用例并启动。首版 Desktop 不支持 DOM 模式，同一桌面一次只运行一个任务。
6. 在任务详情查看截图和动作结果，在 session log 查看 `tool/call` / `tool/result`；结果中的 `executor_used=mac` / `win` 以及 `scout` / `attempts` / 原生元数据可用于核对派单节点和会话。Scout 的进程启动、注册与重连日志在本机，不属于某条用例的 session log。

## 0.1.78 macOS 图形会话检测

macOS 会话探测读取系统返回的 `kCGSSessionUserIDKey`、`kCGSSessionOnConsoleKey` 和 `kCGSSessionAuditIDKey`，登录完成字段为 `kCGSessionLoginDoneKey`。前三项拼写少一个 `S` 会将已登录的桌面误判为 `no_session`。修复源码后，已安装的 app 层还需更新并重启进程才会生效；不能用重新绑定节点或清空配置处理这一问题。

## 0.1.76 连接与桌面诊断

`mino-scout status` 的 `running` 仅表示进程存活；网页在线需要该进程成功连接并注册到 Nexus。检查 `connection.state`、`connected`、`registered`、`last_error` 与最近心跳。`registered` / `heartbeat` 表示完成注册；`registration_rejected` 保留拒绝原因，`stale` 表示诊断超过 90 秒未更新，旧进程未上报时显示 `unknown`。升级磁盘载荷后还需要重启才能使用新诊断。不要因此重新建库、重绑账号或清空配置。

`mino-scout probe` 显示完整设备描述及桌面会话、权限和执行能力。macOS 需要当前登录桌面以及辅助功能、屏幕录制权限；Windows 需要目标用户的交互桌面，SYSTEM 后台任务不能执行 PC 操作。Linux 本版本仅显示真实可用能力，不宣称已实现原生桌面执行。

## 0.1.77 旧插件隔离与节点在线

本版本修正已接受 REGISTER 后因本地旧插件配置待认领而断线的问题：保留节点身份和旧插件文件，节点继续连接并上报心跳；旧插件保持停用，不上传历史插件清单或消息，插件操作返回 blocked。管理员需通过明确的插件归属维护流程处理旧配置，重装或删除配置不是解法。


## 正式与开发 Scout 身份（2026-10-18）

源码运行固定使用 `~/.mino-dev/scout`、独立派生 Scout ID，仅连接本机 Nexus `:50101`；开发 Workbench 为 `:60101`。正式安装使用 `~/.mino/scout`，默认连接 `mino.local:50104`。不要复制正式 config/token/profile 到开发目录。

`python -m mino_scout.cli identity` 只读显示当前入口的 environment、runtime_kind、config_dir、nexus_origin、scout_id、process_build 与 executable。源码/冻结包不单独决定正式/测试：冻结包连接本机 50101 也显示 development；非标准目标显示 custom，不能猜为正式。

`status.configured_identity` 描述本次查询入口，`status.running_identity` 来自正在运行进程自己写入的 sidecar。旧进程尚无该字段时显示空，不能用查询进程冒充其身份。身份不包含 token、URL 用户信息或 query。

开发节点经开发 Workbench 的正常登录/接入流程绑定。开发设备由开发配置选择；不查询正式任务状态，也不以正式环境释放作为开发调试前置条件。


### PyCharm 开发解释器与设备隔离（2026-10-18）

开发 Scout 使用 MinoScout/.venv，按本仓 pyproject 安装依赖；不要用缺少执行侧依赖的 Nexus SDK 直接导入 Scout。
MinoNexus 的共享运行配置 `Scout Development` 经 `MinoScout/scripts/run_development.py` 启动独立解释器。启动器只使用标准库，并 exec 到 Scout/.venv；不需要修改 IDE 的全局 SDK。身份结果保留 executable 原始虚拟环境路径并显示 python_prefix/python_base_prefix，避免符号链接解析后把不同 venv 显示成同一环境。

开发接入命令也使用 Scout/.venv，不再使用 Nexus 的 sys.executable。开发 Nexus 需重启加载更新；正常登录开发 Workbench 后，通过添加节点取得该命令，不复制正式 token。

开发 Scout 不做安卓设备分配白名单：`adb devices` 里处于 `device` 状态的手机都会上报，ADB 执行、IDE APK 安装、启动与心跳输入法维护也不再按序列号过滤。开发与正式的隔离只在进程、配置目录、凭据和 Nexus 地址层面；同一台手机同一时间只应由一个环境使用，这属于使用时的资源安排，不由 Scout 静默拦截（旧版 `development_android_serials` / `development-devices` 已移除，配置里残留该键不生效）。

PyCharm 启动器已完成真实接入：开发 Scout 独立解释器运行，测试 Nexus 注册和心跳成功，插件就绪。执行器直连设备诊断与 Nexus session 业务验收分别记录，不将截图成功当作用例或 APK 安装验收。


## macOS 电池供电合盖运行

Scout 在 macOS 启动时自动检查合盖运行权限，不依赖 `caffeinate -s`。未配置时自动发起首次管理员授权；已有可用辅助服务则直接复用。检查在后台线程执行，不阻塞连接 Nexus。拒绝或取消后，本次进程不再自动弹窗，下次启动可再次请求。用户显式关闭后保留关闭选择，不在启动时重新开启。也保留手动入口：

- `mino-scout configure closed-lid on`：通过 macOS 原生管理员授权安装固定功能辅助服务，然后开启当前开发/正式环境的设置。
- `mino-scout configure closed-lid status`：查看设置和辅助服务实际状态；只有 `active: true` 表示辅助服务已读回系统禁用睡眠状态。
- `mino-scout configure closed-lid off`：关闭当前环境的设置，运行中的 Scout 自动读取，无需重启。

开启后，在 PowerGuard 持有运行租约期间使用 `pmset -a disablesleep 1`，覆盖电池和插电两种情况；这影响整机睡眠而不只是 Scout。首次需要管理员授权，取消则不开启。该功能增加电池消耗，合盖运行时应保持通风，不能放在包内持续运行；不能防止低电量关机、过热保护或系统崩溃。屏幕捕获/桌面自动化在合盖后的显示拓扑变化需要独立验收。

辅助服务仅执行固定系统命令，不以 root 运行 Scout 或用户可修改的 Python。仅接受安装授权用户的存活进程租约；租约每 3 秒刷新，15 秒未刷新后失效。Scout 正常退出、崩溃或释放最后一个 PowerGuard holder 后，在最后一个有效租约结束时恢复原来的 `SleepDisabled` 值（原本为 1 就恢复为 1）。多个开发/正式实例共享系统状态，任一仍持有租约时不会提前恢复。辅助服务由 launchd 保活，重启及机器重启后保留 root 专属恢复记录，检查租约后恢复；仍有有效租约时不短暂开启睡眠；恢复失败保留记录并报告错误。

辅助服务标签为 `com.mino.scout.lid-sleep`，状态和租约位于 `/var/db/com.mino.scout.lid-sleep`。关闭功能后辅助服务保持待机，不持有禁用睡眠设置。另一登录用户不能覆盖原授权；不提供隐式迁移。不要在租约期间同时用其他程序修改同一全局睡眠设置。

源码和静态检查不能代替硬件验收：首次启用后先确认状态 active，再拔电、合盖，检查远端 Nexus 是否持续收到 Scout 心跳及任务结果；重新开盖关闭功能后确认原状态恢复。未经过该验收的机型不可宣称合盖运行已验证。
