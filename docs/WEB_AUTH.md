# Web 用例入口与账号认证快照

业务用例可以跳过与原文无关的登录和菜单导航。登录、退出、权限拦截、菜单导航的专项用例仍走完整路径；URI 不替代创建订单、选择租户等真实前置操作。

## 指定入口

```python
app = case.init(case.config.web)
app.launch(uri="/orders/123?tab=detail#items")
```

`uri` 使用应用配置 URL 解析相对路径，支持完整同源 URL、查询参数和 hash。跨 origin、凭据 URL 和非 HTTP(S) 地址会阻塞。应用配置继续保存环境入口，单条用例不修改它。无参 `launch()` 保留既有行为。

## 一次准备，多条业务用例复用

准备脚本先租指定账号，完成正常登录，然后显式保存认证快照：

```python
account = case.nexus.account()
app = case.init(case.config.web)
app.launch()
# 在 Scout 上完成该账号的实际登录并验证准备结果。
# identity 控件必须唯一，显示完整 username/email/phone；不能用头像证明身份。
app.save_auth(account, identity_control="account.identity",
              identity_field="email", ttl_seconds=3600)
```

业务用例通过 `case_inputs.account_id` 或明确的账号筛选条件租用同一个账号：

```python
account = case.nexus.account()
app = case.init(case.config.web)
app.launch(uri="/orders/123", account=account, auth="required",
           identity_control="account.identity", identity_field="email",
           ready_control="orders.detail", timeout_ms=20000)
# 此后执行原文业务操作和独立业务断言。
```

身份控件与就绪控件使用 pages 中的 CSS、test_id 或 role 确定性定位，也支持已有的 frame/scope 配置。身份必须与所租账号的所选字段完全一致（显示文字去首尾空白）；昵称或遮掩邮箱无法代替完整身份。就绪控件必须唯一且可见，绑定动态对象的场景先通过 `app.bind()` 使用本次实际 ID。恢复完成后校验实际 URI（路径允许末尾斜杠差异，查询参数和 hash 必须一致）；应用自动跳转时应配置最终入口。

## 生命周期与隔离

- Nexus 以项目、应用、账号、环境、渠道、origin 组成快照键；SQLite 保存 AES-GCM 密文，复用既有凭据主密钥。生产主密钥缺失时明确阻塞。
- 每次认证启动都新建独立 browser context，先注入快照再访问目标 URI。浏览器进程可以复用，上一条用例的 context 不复用。
- 当前固定 Playwright 1.49.1：保存目标 origin 的 cookies、localStorage 和当前标签页的 sessionStorage。sessionStorage 通过 Chromium 首次导航脚本注入，随后移除；退出、刷新和后续导航不会重复注入基线。IndexedDB、内存认证、多 origin 的 SSO 存储、设备绑定认证暂不支持；需要重新准备或专门适配，不能报告为已覆盖。
- 快照最大 192 KiB，TTL 为 60 秒到 7 天，默认 1 小时。TTL 表示可尝试恢复的有效窗口，不保证服务端 token 仍有效，仍须验证身份。
- 缺失、过期、已失效、身份错误均阻塞；没有隐式登录、换号、造数或整例重试。身份错误仅失效本次使用的快照 revision；网络、取消和定位故障不推断认证失效。
- 保存仅由 `save_auth()` 显式触发，业务运行结束不会覆盖快照。服务端读写均验证当前账号租约，删除账号同时删除快照。
- 凭据不进入脚本仓、记录、截图附件或公开 API；记录保存快照 revision、请求/实际 URI、验证结果与耗时。页面本身可能展示敏感内容，继续遵守现有截图策略。
- 认证准备记录不会增加业务断言计数，也不能满足原文业务预期覆盖。业务数据隔离与账号独占仍按原租约及准备流程处理。

## 离线检查

`python -B scripts/check_web_auth_contracts.py` 验证 URI、注入顺序、一次性 sessionStorage、失败清理、身份、过期、失效和脱敏。它使用模拟浏览器，不连接目标页面，不代表产品验收或性能基准。Nexus 对应检查为 `tools/check_web_auth_contracts.py`，只使用临时 SQLite。
