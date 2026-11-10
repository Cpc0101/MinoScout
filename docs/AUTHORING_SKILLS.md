# 用例编写、执行与汇报：三端方案

状态：Scout 侧已实现；Nexus 侧见 [AUTHORING_NEXUS_HANDOFF.md](AUTHORING_NEXUS_HANDOFF.md)  
日期：2026-11-04  
agent 技能在兄弟仓库 `../mino`（`skills/`），本文是它们背后的设计。

---

## 0. 结论

| 事项 | 结论 |
|---|---|
| 分工 | Nexus 管用例、设备、账号、任务、会话，**只触发任务**；Scout 自己拉脚本仓库、执行、取证；agent 负责写、跑、汇报 |
| 技能 | 五个，每个只做一件事：`overview` / `repo-config` / `write-test-cases` / `check-test-cases` / `run-test-cases` |
| 本机仓库 | agent 自己用 git；Scout 不提供 workspace 工具 |
| 用例写法 | `app = case.init(case.config.KEY)`；设备动作在 `app` 上；`case.scout` / `case.nexus` 免实例化 |
| 步骤事件即报告 | `case.nexus.do("具体做什么")`，标题写入会话，Workbench 展示；agent 不另写报告 |
| 应用与环境 | 仓库根 `config.yaml`（`apps`，可按 `envs` 分环境）是唯一真源，由 Scout 读取；Workbench 对应字段只读 |
| 应用清单同步 | Nexus 不读仓库；agent 改了 `config.yaml` 并 push 后调 MCP `repo_apps_sync`，Nexus 存清单，Workbench 只读展示，`repo_apps_get` 读取 |
| 自检 | `mino-scout check <repo>` 只给提示；**执行时 Scout 不做任何预检** |
| 设备层兜底 | `app.exec()`，即各渠道已有的 `exec_script` |
| 协议 | `script.run` 不再带 `files`，改带 `repo_url` / `branch` / `revision`；`device_hint` 只有 `env`、`app_id` 和设备标识 |

---

## 1. 职责

| 层 | 负责 | 不负责 |
|---|---|---|
| Nexus | 用例、项目仓库地址、设备与账号及占用状态、任务、会话、事件；触发任务并下发仓库地址 / 提交 / 环境 / 设备 | 拉取或打包脚本仓库、读 `config.yaml`、保存 Git 令牌、执行脚本 |
| Scout | 用本机 git 拉取脚本仓库、读 `config.yaml`、执行、取证、`Case`/`App` 运行时、`mino-scout check` | 选用例、分配账号、决定设备是否空闲、保存业务数据 |
| agent | 读技能、调 Nexus 工具、用 git、写脚本与配置、自检、汇报 | 绕过 Scout 操作设备 |
| Workbench | 展示项目、用例、会话、证据 | 执行、写脚本 |

两处本机 git：agent 在自己的电脑 clone 项目远端写文件；Scout 在设备所在电脑用本机 git 凭据拉同一远端运行。Nexus 不接触仓库内容，私有仓库的权限由各自电脑的 git 凭据决定。

CLAUDE.md 边界：Scout 不碰数据库、不调 Nexus 内部、不读能力目录（`pages/` 与 `config.yaml` 是用例仓库数据，不是 `plugins/**.yaml`）、不写单测（`check` 是命令不是测试）。

---

## 2. 脚本仓库

| 路径 | 放什么 |
|---|---|
| `config.yaml` | 被测应用清单：key、平台、各环境的包名 / bundle id / URL |
| `pages/<APP_KEY>/<page>.yaml` | 页面、锚点、控件、各渠道 locator；一个应用一个目录 |
| `tests/<需求ID>/<case_id>.py` | 用例 `run(case)` |
| `flows/` | 业务流程，函数接收 `app`，可复用 |
| `scripts/` | 纯逻辑辅助，不碰设备 |
| `pretest.py` / `posttest.py` | 项目级钩子 |

`flows/` 与 `scripts/` 的边界由技能说明，运行时不强制。参照：Maestro 的 `runFlow`（编排设备步骤）与 `runScript`（外部纯逻辑）；Robot Framework 的 resource 文件（高层关键字）与 Python library（底层实现）。

`config.yaml`：

```yaml
apps:
  camera:
    app_id: app_8f3a21          # Nexus 里这个应用的 id
    platform: android
    envs:
      test: {package: com.example.a.test}
      prod: {package: com.example.a}
  camera_ios:
    app_id: app_8f3a21
    platform: ios
    bundle_id: com.example.a
```

环境名与 Nexus 项目环境一致。Scout 在拉下来的仓库根读它；`device_hint["env"]` 决定 `case.config` 取 `envs` 里哪一份，`device_hint["app_id"]` 是本次运行的 Nexus 应用 id，脚本里是 `case.app_id`，与 `config.yaml` 各应用的 `app_id` 对应。缺文件、缺应用或缺环境 → `config_missing`。账号不在这里，仍是 Nexus 号池经 `case.nexus.account()` 租用。

---

## 3. 用例运行时（Scout）

`Case` 只有 `config`、`env`、`init`、`scout`、`nexus`。

| 对象 | 内容 |
|---|---|
| `app`（`case.init` 返回） | 启停、点击输入、滑动拖拽、按键、剪贴板、截图、界面树、`on_page`、`vlm`/`vlm_frame`/`sample`、`exec`，以及按渠道的专有方法 |
| `case.scout` | `wait` `otp(account)` `llm` |
| `case.nexus` | `prep` `do` `check`（文字标题）、`account` `note` `fail` |

- 每条 `tool/result` 带 `app`（应用 key）；`exec` 另带 `script_sha256`、`script_length`；
- `pages` 按 `(APP_KEY, page_key)` 隔离；
- 执行器事件与实例方法一一对应，各渠道不支持的方法返回 `declined`；
- 写错 `case.xxx` / `app.xxx`，运行时报错带文件行号、相近名字和可用列表；
- `tap_xy` / `drag` 为视觉方案开放；视觉模型目前只返回文字和 `passed`，**不返回坐标**，“用视觉找位置再点”尚未打通；
- `install` / `uninstall` / `kill` / `read_data` / `set_ime` 按映射全部开放。

### 步骤标题

Scout 对 `do("标题")` 回调传 `title`（传整数时仍传 `number`）。Nexus 以前只认 `number`：它用编号去用例的解析步骤里找原文，写进 `node/enter`，找不到就 `script_step_missing`。标题模式下没有原文可对，Nexus 用递增序号作 `n`，文本取标题。**代价：不再校验“步骤号是否存在于用例原文”，`do` 与 `check` 也不再按同一编号配对。**

一条原文可以拆成多个标题，序号写成 `2-1`、`2-2`。没有前置动作时也写一条 `prep`，标题说明为什么不做，块里可以没有动作。这条标题同样进入 `node/enter`。

---

## 4. 自检与执行

执行路径上**没有预检**：脚本写什么都会跑，唯一保留的是运行时的设备库 import 拦截。

`mino-scout check [目录]`（`runtime/script_check.py`）只读源码，每行 `文件:行号: [类型] 说明`，有提示退出码 1。检查：

| 类型 | 内容 |
|---|---|
| `forbidden` | 设备库、`subprocess`、线程 / 进程 / asyncio、`exec`/`eval`/`os.system`、`if platform` |
| `reuse` | `time.sleep`、`imaplib`/`poplib`、`openai`/`anthropic`（Scout 已有对应能力） |
| `case_attr` / `case_init` / `step_title` | 旧写法、`case.init` 参数、步骤没标题 |
| `prep_missing` / `prep_reason` | `tests/` 下的用例没有 `prep`；标题只写了「无」 |
| `config_*` / `page_layout` | `config.yaml` 缺失或格式、脚本引用了不存在的应用、页面文件位置 |

它不检查方法名（变量名不固定），也不能保证脚本能跑通。

**已知风险**：执行时不拦截后，`threading` 或死循环会让脚本挂住。超时只在每次 `app.*`/`case.*` 调用时检查，全局 `_RUN_LOCK` 串行所有脚本，挂住的脚本会让该节点后续脚本全部排队。根治办法是把用户脚本放进子进程并在超时时 kill，本方案不做，是否做取决于线上是否出现。

---

## 5. 执行

1. agent push 后 `case_run(case_id, sn, revision=<sha>)`（`revision` 可省，省略取分支最新）；
2. Nexus 准入（设备、账号、租约）后，向设备所在 Scout 下发 `script.run`：`repo_url`（去内嵌凭据）、`branch`、`revision`、`script_path`（可空）、设备 `sn`，`device_hint` 带 `env`、`app_id` 和设备标识；
3. Scout 用本机 git 拉取（缓存 bare 仓库 + `git archive` 出干净副本），在 `tests/**/<case_id>.py` 里找脚本，读 `config.yaml`，执行，并写一条 `checkout` 证据记录实际提交；
4. 取不到：`repo_unavailable`（检查本机 git 凭据）、`revision_missing`（确认已 push，不回退分支最新）、`script_missing` / `script_ambiguous`、`config_missing`。

任务级状态不新增值：`declined` / `skipped` 聚合为 `fail`，`cases[].status` 保留这两个值。Nexus 不保存 Git 令牌。

**选设备**：设备的在线、占用、能否立即使用由 Nexus 的设备列表给出（见 `run-test-cases`），Scout 不自己判断设备是否被别的任务占用，也不自己选设备。

---

## 5.1 应用清单同步

Nexus 不读脚本仓库（可能是私有的），所以“这个项目的仓库能跑哪些应用”由 agent 告知：

- `repo_apps_sync(project_id, revision, branch, apps)`：差异更新：`apps` 只带本次新增或修改的应用（按 key 整项替换），`remove` 带删除的 key，其余保留；校验 key 格式、每项必须有 `app_id`、platform、只允许 `package` / `bundle_id` / `url` 与 `envs`，含 password / token / otp / secret / key 等键名一律拒绝；
- `repo_apps_get(project_id)`；
- Nexus 存 `project_repo_apps`（revision、branch、apps、同步人、时间），Workbench 的“脚本仓库应用”只读展示；
- 约定写在技能里：改了 `config.yaml` → push → 同步 → `repo_apps_get` 核对 revision；
- 执行时仍以 Scout 拉到的仓库为准，清单只用于展示和选应用，所以最坏情况是展示过期，不会跑错应用。

## 6. 未做 / 待定

- **清单 key 与 Nexus 应用的对应（已定：方案 B）**：`config.yaml` 每个应用带 `app_id`（Nexus 里该应用的 id），`case_run` 用它匹配清单；key 自由命名（英文字母开头，含英文、数字、下划线）。应用先用 MCP `app_create` 在 Nexus 创建拿到 id；
- `device_list` 的兼容判断仍按 Nexus 原有的应用和渠道环境，与清单无关；

- 视觉返回坐标（`app.vlm_point`）；
- 多设备 / 多渠道在同一用例（需要 `case_run` 按应用绑定设备、`script.run` 增 `targets`，是协议变更）；
- 脚本子进程隔离与强杀；
- Workbench：项目配置里包名 / URL 改只读；`cases[].status` 新值展示；
- 存量老脚本（`case.tap` 写法）需迁移。
