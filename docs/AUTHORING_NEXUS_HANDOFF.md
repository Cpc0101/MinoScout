# Nexus 侧改动记录：用例编写 / 执行

状态：代码已改，**未联调、未提交**，待 Nexus 评审  
日期：2026-11-04  
对应方案：[AUTHORING_SKILLS.md](AUTHORING_SKILLS.md)

原则：**Nexus 只触发任务，不参与脚本仓库的拉取、打包、检查和执行**。Scout 用本机 git 自己拉仓库、读 `config.yaml`。以下均做过 `py_compile`，没有跑测试（Nexus CLAUDE.md 约束 5）。

## 1. 已改

| # | 内容 | 位置 |
|---|---|---|
| 1 | `ScriptRun` 改为 `run_id, session_id, case_id, sn, platform, repo_url, branch, revision, script_path`，去掉 `files`。`repo_url` 必填且不得带内嵌凭据；`branch` / `revision` 不得以 `-` 开头或含空白；`script_path` 可空，非空须是仓库内 `.py` | `core/protocol.py`；`docs/基础框架/PROTOCOL.md` |
| 2 | 下发只组装载荷：`repo_url` 去凭据，`revision` 取 run 记录（不透明，Nexus 不校验）。`device_hint` 只有 `env`（`env_profile`）、`app_id`，加设备记录已有的标识（Android：`adb_serial` 取设备 `channels.serial`、无则 `sn`，`model`；iOS：`udid`=`sn`） | `loop/script_dispatch.py` |
| 3 | `case_run` 的 `revision` 原样存进 run 并由 `task_get` 返回，不再克隆 / fetch 校验；取不到由 Scout 返回 `revision_missing` | `api/rCaseRunner.py`、`loop/case_runner.py` |
| 4 | 删除服务端打包与检查：`script_config.py` 整文件、`script_bundle.py` 里的 `collect_files` 与大小限制（保留 `locate_case_script`，Workbench 查看 / 编辑脚本文本要用）；`script_workspace.py` 恢复到改动前（去掉 `verify_revision`、完整克隆）；去掉 `flows/` 白名单、`config_missing`、`script_pack_*` | `casework/` |
| 5 | 步骤回调支持 `title`（递增序号作 `n`），`tool/call` / `tool/result` 保留 `app`、`script_sha256`、`script_length` | `loop/script_callback.py` |
| 6 | `project_list` 增 `repo_url`（去凭据）、`repo_branch`；`case_list` 补 `requirement_id`、步骤不再截断 | `resources/project_store.py`、`casework/case_store.py` |
| 7 | 删除 `script_guide`；`agent_guide` 指向五个技能；`declined` / `skipped` 在用例级保留，任务级仍聚合 `failed` | `api/rResourceView.py`、`assistant/agent_guide.py`、`loop/script_dispatch.py`、`casework/run_store.py` |
| 8 | V9 文档里 `script_guide` 与"Nexus 打包 files"的描述已删 | `docs/架构/V9/` |

## 1.1 应用清单同步（已改，未联调）

| 内容 | 位置 |
|---|---|
| 新表 `project_repo_apps`（project_id、revision、branch、apps、synced_by、synced_at），`SCHEMA_VERSION` 6→7，旧库启动自动补建 | `models/project_repo_apps.py`、`core/schema_migrations.py` |
| `GET` / `PUT /project/{project_id}/repo-apps`（PUT 需 `project_maintainer`，整份覆盖；校验失败 422 `repo_apps_invalid`） | `api/rProject.py`、`resources/repo_apps.py` |
| 差异更新：`apps` 按 key 新增 / 整项替换，`remove` 删除，其余保留 | `resources/repo_apps.py` |
| MCP 工具 `repo_apps_sync` / `repo_apps_get`，新增 `app_create`（POST /project/app/create） | `prompts/assistant/tools/repo.apps.*.yaml` |
| `case_run`：项目有清单且传了 `app_id` 时，要求某个清单条目的 `app_id` 等于它（精确匹配），否则 400 `app_not_in_repo_config`；无清单不校验。清单 key 自由命名，每项必须带 `app_id` | `api/rCaseRunner.py` |
| `agent_guide` 补同步约定 | `assistant/agent_guide.py` |

风险：`revision` / `branch` 只记录不校验；`project_maintainer` 对新路由的取权行为未单独验证。

## 2. 设备选择（`device_list`，未改，只调查）

`GET /case-runner/devices`。默认只返回当前登录账号名下的设备。入参都可选（`app_id`、`env_surface`、`env_profile`、`only_online` 等）；`project_id` 在工具声明里有，路由函数没有，传了无效。

| 字段 | 含义 |
|---|---|
| `status` | `online` / `offline`；`node_alive` 表示所属 Scout 节点是否在线 |
| `busy_task_id` | 占用该设备的任务，空串=空闲；web 槽位恒为空串，改看 `active_run_count` / `web_parallel_max` / `web_parallel_full` |
| `executable` | 现在能否立即使用 |
| `blocking_code` / `blocked_reason` | 原因：`unclaimed` `not_owned` `node_offline` `channel_offline` `desktop_unreported` `capability_missing` `platform_unsupported` `app_incompatible` `busy` 等 |
| `isolation` | 设备被隔离时的状态 |

`run-test-cases` 技能据此选设备：只选 `executable=true` 且平台一致的。

## 3. 未做 / 需要关注

- **Workbench 已改（只做了 tsc / eslint，没在浏览器看过）**：应用运行配置的“启动标识 / 地址”输入框只读（Server 渠道 `base_url` 保持可编辑，Nexus 后端写入口未拒绝）；新增“脚本仓库应用”只读区块；用例状态 `declined`（Scout 拒绝执行）/ `skipped`（未执行）；任务统计新增两项；任务信息抽屉显示脚本版本和仓库。步骤级状态标签未改。
- **Nexus 仍保留仓库副本**：`script_workspace.ensure_workspace` 等被 Workbench 展示 / 编辑仓库页面（`page_files.py`、`rProjectCases.py`）和旧链路使用，没有删；它不参与脚本执行。
- **旧链路** `loop/script_case.py`、`script_bridge.py` 仍依赖 `sync_workspace`，未确认是否还在被调用。
- **`app_id` 语义（已定）**：Nexus 下发的是本次 run 的 Nexus 应用 id，对应 `config.yaml` 各应用的 `app_id` 字段（不是 key）。Scout 里是 `case.app_id`，`case.config.<KEY>` 的 `AppRef` 也带 `app_id`。
- **平台名**：下发的 `platform` 用 run 的平台，未逐个入口核对已是 `mac` / `win` 而非 `desktop`。
- **`request_digest` 含 `revision`**：部署瞬间用同一 `client_request_id` 重试可能被判 `run_request_conflict`。
- **步骤标题**：不再校验步骤号是否存在于用例原文，`do` / `check` 不再同号配对。
- Nexus 工作区原有大量未提交改动，与本次改动混在一起，提交前需分开审阅。

## 4. 评审清单

- [ ] `script.run` 不再带 `files`，两仓 `protocol.py` / `PROTOCOL.md` 一致
- [ ] Scout 能凭 `repo_url` / `branch` / `revision` 拉到仓库并执行（含私有仓库）
- [ ] `device_hint` 只有 `env`、`app_id`、设备标识
- [ ] `script_guide` 已删除，`agent_guide` 指向五个技能
- [ ] `project_list` / `case_list` 新字段可用
- [ ] `cases[].status` 保留 `declined` / `skipped`，任务级仍为 `failed`
- [ ] Workbench 包名 / URL 只读，“脚本仓库应用”能显示同步结果
- [ ] `repo_apps_sync` / `repo_apps_get` 经 MCP 可调用
