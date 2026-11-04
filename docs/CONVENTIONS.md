# CONVENTIONS — MinoScout

## 1. 日志

沿用上游 `SLog` 的形态：模块级 `TAG` 常量 + `SLog.{d,i,w,e}(TAG, msg)`。

```python
TAG = "AdbExecutor"
SLog.i(TAG, f"tap ({x},{y}) serial={serial}")
```

| 规则 | 说明 |
|---|---|
| TAG 用驼峰模块名 | `AdbExecutor` / `WdaExecutor` / `NodeTransport`，不带路径 |
| 一行一事 | 不要多行拼接；trace 归 Nexus，Scout 的日志只服务本机排障 |
| **不记凭据** | `EXECUTE.device_hint` 里的 `password`、`session_token`、`auth_token` 一律不入日志。需要时打 `password=***` |
| **不记截图 base64** | 打长度即可：`shot bytes=182344` |
| 每条 `EXECUTE` 记一行结果 | `run_id` 前 8 位 + `step_idx` + cap + executor + status + 耗时，便于和 Nexus 的 trace 对齐 |

## 2. 异常与五态（本仓最重要的约定）

**Executor 永不抛异常。** `execute()` 必须捕获自身全部异常，转成五态之一。取值**小写**，且是 `pass` 不是 `OK` —— 与上游 `EventStatus` 逐字一致。

| 状态 | 什么时候用 | 例子 |
|---|---|---|
| `pass` | 动作做成了 | 不要在"发出去了但不知道成没成"时用 `pass` |
| `fail` | 真故障 | 设备离线、坐标越界、shell 返回非 0 |
| `declined` | 我不适合做这条，让位 | `playwright_executor` 收到 Android 专属 cap；`ios_wda` 收到清应用数据 |
| `blocked` | 需要人介入才能继续 | 需人工确认的系统弹窗、设备被锁且无密码 |
| `skipped` | 本次未执行 | 前置条件不满足，压根没动手 |

三条推论：

- **任何状态都结束本次尝试。** Scout 不切换 executor、不改序、不追加通道；`blocked` / `declined` / `fail` / `skipped` 都原样回报，由 Nexus 重新选路。
- **`declined` 与 `fail` 的区别是"算不算故障"**，不是"换个 executor 有没有用"。写错会污染失败统计和归因。
- **`declined` 必须便宜。** 判断要在真正操作设备**之前**做出（`supports()` 或 `execute()` 开头），不要先折腾 20 秒再 `declined`。

这条合同叫 V7：`EXECUTE.executor_order` 必须恰好有一个 executor，`selected_impl.executor` 必须与之一致。上游的 fallback 行为不再适用。

## 3. 坐标

**协议里的坐标一律是 0–1000 归一化千分比，不是像素。**

换算在 Scout 侧做，靠当前屏幕实际分辨率：

```python
px = round(x_permille * width  / 1000)
py = round(y_permille * height / 1000)
```

不要在 Nexus 侧换算 —— Nexus 不该知道设备分辨率。不要把像素坐标回传到 `RESULT` 里当权威值。

## 4. 超时

| 层 | 规定 |
|---|---|
| 协议给的 `timeout_sec` 是**硬上限** | Scout 内部所有等待加起来不得超过它 |
| 每个 executor 内部再留 20% 余量 | 便于在超时前返回一个有意义的 `fail` 而不是被外层掐断 |
| 机械重试计入总时长 | 不允许"重试 3 次每次 30 秒"把 30 秒的预算撑到 90 秒 |

## 5. 幂等

`(run_id, step_idx)` 是唯一键，唯一实现在 `runtime/request_guard.py`：

- 登记完整请求内容。相同键不同内容 → `request_key_conflict`；在途重复 → `request_inflight`
- 已完成 → 直接回缓存的 `RESULT`，不重新执行
- 写动作结果未知 → 封存本次结论（晚到的结果不能改写），并给该 run 打停止闩；后续写动作一律拒绝
- 缓存结果与停止闩都在进程内存，Scout 重启即丢 —— 这是可接受的，Nexus 重发会重新执行

`cancel_run` 的语义是“不再继续”，不是回滚，也不负责清空幂等缓存。

## 6. 守门脚本（当前不存在）

上游有 45 个 `scripts/verify_*.py`，本仓**没有搬**，`scripts/` 里只有打包与分层安装检查。
CI（`.github/workflows/release.yml`）只构建产物，**不跑 Python 测试、不做静态守门**。

因此 CLAUDE.md §1 的五条硬约束目前靠 **review + 自查**，不靠脚本。新增代码时必须自己核对：

| 约束 | 自查方式 |
|---|---|
| 不碰数据库 | `grep -rn "sqlalchemy\|mino_nexus" mino_scout` |
| 不调大模型 | 只有 `server_comm/model_call.py`（脚本模式）允许；其余目录不得出现 LLM endpoint / prompt |
| 不读能力目录 | 不解析 `plugins/*.yaml`，不出现 `abstract_caps` |
| 不 import Nexus | 不出现 `mino_nexus` / `server.*` |
| 不写单测 | 不新增、不改 `tests/`；执行侧以 Nexus `session_events` 为准 |

如果以后要补守门脚本，按上表一约束一脚本写，**不要在文档里提前宣布它们存在**。

## 7. 提交与 PR

- 协议改动必须两仓同 PR 周期完成，四步流程见 `CLAUDE.md` §5
- 从上游搬文件的 commit message 注明原路径：`port(scout): screen.py from MiniOrangeServer server/services/regression/screen.py`
- 不在同一个 commit 里既搬迁又重构。**先原样搬（保证行为一致），再单独 commit 改造**
