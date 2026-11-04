> **唯一前端硬约束（2026-10-14）**：Mino 项目只有 MinoWorkbench 一个前端，MinoConsole/MinoStudio 已取消。不得新增、开发、构建或交付旧前端；管理、配置、用例和证据界面全部在 Workbench。历史字段与安装路径名称仅为兼容，不代表旧前端仍存在。

# CLAUDE.md — MinoScout

本文件是给 Claude Code / 任何在本仓库工作的人的**硬约束清单**。写代码前先读这里。

---

## 0. 本仓库的定位

MinoScout 是执行器。它接收 [MinoNexus](../MinoNexus) 下发的动作，操作真实设备，回报结果。

上游来源是 [MiniOrangeServer](../MiniOrangeServer)（**已停止维护，只读参考，禁止改动**）。搬迁映射见 [docs/MIGRATION.md](docs/MIGRATION.md)。

---

## 1. 五条硬约束（违反即 CI 失败）

| # | 约束 | 守门脚本 | 为什么 |
|---|---|---|---|
| 1 | **不碰数据库** —— 不 import `sqlalchemy`、不定义 ORM 模型、不写迁移 | — | 数据归属方是 Nexus。Scout 有状态就没法随时重启、没法多节点 |
| 2 | **不调大模型** —— 不 import `openai` / `httpx` 打 LLM endpoint、不含 prompt 常量 | — | 决策权在 Nexus。Scout 一旦能"想"，两边就会各想一半 |
| 3 | **不读能力目录** —— 不解析 `plugins/**.yaml`、不含 `abstract_caps` 逻辑 | — | 能力目录唯一真源在 Nexus。Scout 只按 `EXECUTE` 载荷里给定的 `executor_order` 执行 |
| 4 | **不 import Nexus** —— 任何 `mino_nexus` / `server.*` 引用 | — | 依赖必须单向 |
| 5 | **不写/不改单元测试** —— 禁止新增 `tests/test_*.py`、禁止为自证修复跑 pytest | Agent 擅写 test 干扰现场；执行侧问题以 Nexus **`session_events`**（`tool/call`、`tool/result` 的 `summary`）为准 |

边界说明：

- 约束 3 的对象是 `plugins/**.yaml` 能力目录。`pages/<APP_KEY>/*.yaml` 是用例脚本仓库里的页面数据，由 `script_exec._load_pages` 读取，不是能力目录；`mino-scout check`（`runtime/script_check.py`）只读用例源码给 agent 提示，不读能力目录、不决定 `executor_order`。
- 约束 5 禁止的是 `tests/test_*.py` 和 pytest。`mino-scout check` 是写用例时给 agent 的自检命令，不是单测，执行时 Scout 不做任何预检。


---

## 2. 两条铁律

### 2.1 Executor 永不抛异常

```python
def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
    ...
```

必须捕获自身所有异常并返回五态之一。**取值是小写，且是 `pass` 不是 `OK`** —— 与上游 `EventStatus` 逐字一致，避免搬迁时到处翻译名字。

| 状态 | 语义 | Router 行为 |
|---|---|---|
| `pass` | 做成了 | 返回 |
| `blocked` | 需要人介入（Scout 不问人，上报 Nexus） | 返回，不换路 |
| `declined` | 我不适合做这条，让位 | 返回本次结果，不切换 executor |
| `fail` | 真故障（设备报错、超时、元素找不到） | 返回本次结果，不切换 executor |
| `skipped` | 本次未执行（前置不满足等） | 返回本次结果，不切换 executor |

**当前 V7 合同**：EXECUTE 必须选定唯一执行器（`executor_order` 长度恰好 1，且
`selected_impl.executor` 与之一致）；任何状态都结束本次尝试。禁止失败后切换执行器，
未知写结果仍需隔离。上游的 fallback 行为不再适用。

### 2.2 不做隐式回退到"另一种连接方式"

`ScoutCore` 内部**禁止出现 `if 本地 / if 远程`、`if 单机 / if 集群` 这类分支**。连接形态是外层 transport 的事，core 只认 `ExecuteRequest → ExecuteResult`。

同理，设备驱动多方案（adb / WDA / Playwright / macOS·Windows）必须**可插拔**，不允许 `if backend == "appium"` 式的分叉；新增一种驱动是新增一个 executor 或平台模块，不是在现有分支里加一个 `elif`。

---

## 3. 目录约定

```
mino_scout/
├── cli.py              入口：run / probe / status / stop / start / configure
├── service.py          pid / 单实例锁 / 启停
├── config.py           配置目录、scout_id、环境隔离
├── core.py             ScoutCore：execute / observe / manifest / heartbeat
├── router.py           唯一执行器分发 + 五态 + 写边界归一化（不做选路）
├── schemas.py          PlanEvent / EventResult
├── screen.py           按平台分派截图（不做多通道回退）
├── case_sources.py     用例数据源读取（xlsx / csv / API）
├── workspace.py        编写工作区：本机 clone / fetch / checkout
├── server_comm/
│   ├── protocol.py     5 种消息 + 载荷 dataclass（stdlib，零依赖）
│   ├── transport/node.py   daemon 形态：反向 dial Nexus 的 WS 客户端
│   ├── script_dispatch.py  script.run 载荷校验 / 租约 / 落盘
│   ├── callbacks.py    账号 / 验证码 / 步骤 / 失败回调
│   ├── nexus_link.py   脚本 → Nexus 的请求
│   └── model_call.py   脚本模式的本地文本/视觉模型（见 §1 约束 2 的例外）
├── runtime/
│   ├── request_guard.py      幂等 / 在途 / 停止闩
│   ├── execution_boundary.py 写动作三值回执
│   ├── execution_binding.py  连接/进程代际与能力修订
│   ├── platform_contract.py  目标平台与设备身份
│   ├── script_exec.py        用例脚本运行时（Case API）
│   └── secret_redaction.py   出口脱敏
├── executors/          adb / ios_wda / playwright / mac·win 原生 + base
├── platform/           android / ios / web / mac / win 的原始动作
├── plugins/            插件运行时、IM / OTP / CLI 桥
└── tools/              keep_alive（电源/日志）+ update（分层升级）
```

agent 技能（总览 / 仓库配置 / 编写 / 自检 / 执行汇报）不在本仓，放在兄弟仓库 `../mino`（`skills/overview`、`repo-config`、`write-test-cases`、`check-test-cases`、`run-test-cases`）。

设备通道：`adb`（Android）/ `ios_wda`（iOS）/ `playwright`（Web）/ `mac`、`win`（原生）；另有宿主插件面
`host_plugin`。**`remote`（ClawNode）已于 2026-11 删除，不要恢复。**

## 4. 改动前必读

| 你要做什么 | 先读 |
|---|---|
| 加一个能力 / 改一个动作的执行方式 | [docs/EXECUTORS.md](docs/EXECUTORS.md)。**注意：capability 的声明在 Nexus 的 `plugins/`，本仓只实现** |
| 加一种设备驱动 | [docs/ENGINES.md](docs/ENGINES.md)（现行没有 EngineFactory） |
| 改与 Nexus 的通信 | [docs/PROTOCOL.md](docs/PROTOCOL.md) + §5。**协议改动必须两仓同步** |
| 从上游搬代码 | [docs/MIGRATION.md](docs/MIGRATION.md) |
| 改打包 / 安装 / 更新逻辑 | [docs/PACKAGING.md](docs/PACKAGING.md)。**发布产物是分层的**，改之前先看那里的层指纹与闸门 |

---

## 5. 协议同步规则

协议在两个仓库各有一份 `protocol.py`（本仓 `mino_scout/server_comm/protocol.py`，Nexus `mino_nexus/protocol.py`）。**没有共享包** —— 只有两个仓库，不引入第三个。

契约真源是两仓同步的 `docs/PROTOCOL.md` 和对应 `protocol.py` 实现；`tests/fixtures/protocol/*.json` 仅供本地调试，不提交 Git。

改协议的流程，**同一个 PR 周期内完成**：

1. 改 `docs/PROTOCOL.md`（两仓同一份文本）
2. 两仓各自改 `protocol.py`，使其与文档一致
3. 若本地有 fixture，两仓各自确认 round-trip 并保持本地样例一致，不提交样例

`docs/PROTOCOL.md` §8 的哈希仅供有本地 fixture 时核对；两仓协议文档或实现不一致 = 协议漂移。

---

## 6. 不要做的事

- 不要为了"顺手"在 Scout 里读一次数据库 —— 需要的数据让 Nexus 塞进 `EXECUTE` 载荷
- 不要在 Scout 里判断"这一步该不该做" —— 那是 Nexus 的循环
- 不要把 `plugins/*.yaml` 复制进本仓
- 不要改 `../MiniOrangeServer` 的任何文件
- 不要新增对外监听端口；Scout 永远是 dial 出去的一方（ClawNode 已删，也没有设备侧 WS 服务端）
- 不要在 `RESULT` 里省略 `executor_used` —— Nexus 的 trace 和覆盖度统计依赖它
