# 确定性用例、模型策略与业务报告

脚本声明 `RUN_PURPOSE`（authoring/verification/regression，缺省 regression）与字面量 `MODEL_POLICY`。模型缺省关闭；旧 `expect(page=...)` 继续验证页面，`expect(prompt=...)` 需要 `allow_model=True` 和显式原文关联策略。固定文字/状态/数量/值迁移为 `expect_text/state/count/value`，不能删掉业务预期以获得零模型运行。

`MODEL_POLICY={"mode":"allow","max_calls":1,"timeout_ms":15000,"total_timeout_ms":15000,"allow":[{"kind":"vlm","source_ref":"E1","purpose":"visual_assertion","reason":"原文要求复杂画面语义验证","max_calls":1}]}`。LLM 对应 text_assertion；VLM 描述对应 exploration，仅在 authoring 允许。全部调用经统一门禁，绑定当前作用域引用，失败也消耗次数，网络超时受单次/总预算和用例剩余预算限制。model/call/result 记录用途、耗时与服务返回 token，不保存模型密钥或请求图像。

确定性断言精确比较原生观察；`wait_for` 只轮询读操作，最终产生一次 assertion/result。截断观察不能证明总数或不存在，缺状态不能猜；读取秘密内容始终未核验。iOS XML、桌面 JSON 可访问性树在运行时归一化，原始记录仍保留。

OCR 是执行 Scout 的可插拔本地观察后端：macOS Vision（Swift 编译器，首次编译计入预算，按源码与系统架构指纹缓存，后续用例复用），其他宿主 Tesseract CLI（需预装及匹配语言包）。没有新增 torch/paddle/opencv 或云端图像依赖。后端接收当前应用截图并返回文字、置信度和像素边框，region 为归一化 [left,top,right,bottom]。无引擎、识别不足、多候选或画面变化明确 blocked。`ocr_text` 是显式页面选择器，原生失败后不自动换路径；相对目标需要文字和 left/right/above/below 关系，禁止猜无字图标。

Web OCR 使用当前视口和明确 viewport_pixels 坐标；iOS 转为 WDA 点；Android 明确使用 screenshot_pixels，按截图与屏幕尺寸换算，避免小像素坐标被误当千分比；桌面使用绑定截图的千分比坐标。OCR 完成后重新确认目标区域像素稳定，再进入已有平台写边界。执行器、设备隔离和租约规则不变。

完成时 Scout 从实际原文、断言与事件生成 test_report：按预期聚合 pass/fail/unknown/not_run，保留失败优先级、证据序号、预期/实际、执行阶段、首个失败作用域及模型消耗。suspected_product 仅表示业务差异待归因，不能宣布产品缺陷；定位/脚本/策略错误归自动化，资源/观察不足归阻塞。索引最多展开 50 条，完整事件仍归执行 Scout。Nexus 两种部署只接收有界索引，Workbench 默认展示业务结论，按需查看执行诊断。断言总数与业务预期数分别统计。

验证使用隔离临时目录 smoke、原生 OCR 合成图片和 Workbench API fixtures；不访问团队业务库、不跑 pytest。真机验收与实际性能测量单独完成。
