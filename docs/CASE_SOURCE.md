# 从飞书读取用例原文

飞书维护前置、步骤和预期，Python 维护自动化实现。新用例不需要同名 YAML。在脚本顶层声明一次字面量字典：

```python
CASE_SOURCE = {
    "kind": "feishu_sheet",
    "url": "https://example.feishu.cn/wiki/TOKEN?sheet=SHEET_ID",
    "sheet_id": "SHEET_ID",
    "range": "A2:H1000",
    "case_id": "order_total",
    "columns": {
        "case_id": "A",
        "preconditions": "D",
        "steps": "E",
        "expectations": "F",
    },
}
```

`url` 支持飞书 wiki 表格链接和 sheets 链接。指定工作表（`sheet_id` 或 URL 的 `sheet` 参数）、有限的 A1 范围、稳定用例 ID 及绝对列字母。指定范围内必须唯一匹配用例 ID。行移动后只要仍在范围内就能找到；列调整后更新列位置。当前来源类型支持飞书电子表格，不把文档链接自动猜成表格。

每个前置、步骤、预期单元格中一条条目占一行。建议在飞书正文中写稳定编号 `P1: 前置`、`S1: 操作`、`E1: 预期`；也支持中文冒号。不显式编号时按非空行顺序生成 P1/S1/E1 等编号。不要依赖换行排版一个条目，后续增加条目应保留原编号。Python 的 `case.nexus.prep/do/check` 用 `source_ref` 关联这些编号。

执行前 Scout 复用本机已配置的飞书读取能力，连续读取两次核对一致性，然后冻结本次选定用例的原文、内容哈希、位置与读取时间。快照只归档在执行记录中，Workbench 继续从该次记录展示原文，不读取后来被修改的飞书内容。`source.revision` 为所选原文内容哈希，`source_hash` 不受读取时间变化影响。

原文读取使用执行节点自己的飞书授权。权限不足、网络失败、来源不完整、重复用例 ID、缺少预期、原文编号重复或运行引用不符均阻塞，不退回历史 YAML。飞书调整了操作或预期后，自动化实现及 `source_ref` 仍需相应核对；自动读取不等于自动修改业务断言。

静态 `mino-scout check --require-cases .` 用 AST 读取 `CASE_SOURCE`，不导入脚本、不调用飞书。验证配置后为每条 `CASE_SOURCE` 给出 `source_remote/info`，说明原文映射与覆盖必须在执行时核验；这是静态检查的能力边界，不是待修复问题，也不会阻塞静态门禁。运行时仍会核验原文映射与覆盖。失败读取返回 `source_read_failed`，没有开始设备动作时保持未执行。

旧同名 YAML 保留兼容。单条用例迁移到 `CASE_SOURCE` 时删除同名 YAML；两者并存视为配置冲突。`config.yaml` 和页面定位 YAML 不属于用例原文快照，继续保留。
