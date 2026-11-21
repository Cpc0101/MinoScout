# Web 请求关联与浏览器错误证据

当前源码提供 Web 运行时 API，执行仍由 Scout 完成。旧版节点不因服务端文档更新而获得新能力。

## 请求与响应

```python
with app.network.expect_response(
    method="POST", path="/api/orders", timeout_ms=10000,
    capture_fields=["code", "data.id"],
) as response:
    app.tap("orders.submit")

response.expect.status(201)
response.expect.json_field("code", "SUCCESS")
# 页面结果仍需独立业务断言。
```

先注册期望，再执行动作，再等待响应。只匹配注册后开始、属于注册时当前页面的请求；path 精确匹配，不会把 `/api/orders-old` 当作 `/api/orders`。method、url_contains、status 和 json_fields 可显式约束匹配；url_contains 只对去掉查询参数的地址生效。跨 origin 接口须提供可区分的地址条件。同页相同路径的多请求建议用业务字段匹配。

occurrence 默认 None：取得响应时已有多个候选会以 network_expectation_ambiguous 阻塞。显式 occurrence=N 按符合条件的响应观察顺序选第 N 条，跨多个事件分发轮次累计；这不是请求发起顺序，也不能保证未来不会出现另一条同类响应。注册窗口与动作只有时间关联，不声明因果关系。

HTTP 状态断言和 JSON 业务字段断言分别记录，匹配成功本身不算用例通过。status/json_fields 用作筛选时，不匹配的响应可能导致等待超时；需要明确识别业务失败时，应先按请求特征匹配，再执行断言。

## 正文与边界

正文默认不采集。capture_fields 和 json_fields 明确列出允许采集的路径，首版支持标量字段；password/token/cookie/OTP 等敏感字段禁止采集，已登记密钥继续脱敏。未声明字段、缺正文或正文超限的断言是 unknown，不猜结果。

正文只在 requestfinished 后读取，避免 response.body 在慢响应上阻塞完整等待窗口。首版要求 JSON Content-Type、有效 Content-Length、不超过 64 KiB 且无压缩；缺长度、流式/压缩正文明确报告不可用。Playwright 的 body API 本身读取完整已完成正文，本实现通过头部准入和读取后检查限制证据量，并非流式读取器。正文不满足条件仍可执行 HTTP 状态断言。

最多保留最近 250 条响应；期望注册后发生淘汰时返回 network_observation_overflow，不在缺证据时选择剩余一条冒充唯一。等待使用真实单调时间，浏览器事件分发持续进行；运行取消与执行超时优先停止等待，finally 清除期望。动作抛出异常时尝试取消期望并保留原异常。

WebSocket、流式消息、Service Worker 中无法取得页面归属的请求首版不匹配。不同角色/页面不可相互满足期望。

## 浏览器错误

首次导航前监听 context 请求事件和页面异常/console error，新标签页也安装监听。HTTP 4xx/5xx、requestfailed、pageerror、console error 分开记录。证据自动写入 Scout 的 browser/error，无需业务脚本额外调用；app.browser_errors() 可读取或清空保留列表，不能删除已落盘证据。

browser/response 保存已匹配响应的请求标识、方法、路径、HTTP 状态、字段证据和时间关联。browser/observation 保存错误保留数、丢弃数和落盘失败数。每页标签和角色上下文可追溯；时间上邻近某步骤不证明它是步骤失败根因。

最多保留 100 条错误，超出的数量明确可见。错误为诊断事实，默认不改变用例结论；接口/页面业务断言决定通过或失败。Workbench 显示浏览器错误与接口响应详情，HTTP 201 等数字不作为执行通过状态。

完整记录保留在 Scout，Nexus 仅保存任务索引并代理证据读取。节点离线时证据暂不可用，不将缺证据解读为没有错误。

## 检查

`python -B scripts/check_web_observation_contracts.py` 验证事件分发、旧请求排除、页面隔离、分批响应、歧义、取消、字段白名单、正文限制和错误丢弃计数。`scripts/check_web_auth_contracts.py` 验证入口与认证兼容。这些是离线模拟，不代表真实浏览器验收或产品通过。
