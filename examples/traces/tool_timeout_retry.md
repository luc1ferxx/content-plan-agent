# 运行记录：tool_timeout_retry

> 离线 Mock：决策由固定规则产生，只验证流程，不证明真实模型的决策效果

- 决策模块：`mock`
- 结果状态：**completed**
- 费用合计：14 / 预算 14
- 用量：{"tool_calls": 5, "model_turns": 5, "max_tool_calls": 15, "max_model_turns": 30}
- 已保存方案：CP-43ea252c（版本 1，created）

## 执行轨迹

| 步骤 | 触发（观察） | 动作 · 决策者 | 决策依据 | 工具 | 参数 | 返回摘要 | 费用（累计） | 模拟时钟 | 后续动作 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 初始规划 | `plan:P1` · mock | 选择效果优先的 P1 旗舰方案：AI 分镜短视频，先调用成本估算确认预算与截止时间 | `estimate_cost` | `{"option_id": "P1", "spent": 0, "clock": "2026-11-05 09:00", "items": ["estimate_cost", "search_materials", "generate_script:premium", "save_plan"]}` | 成功：P1 预计合计 12，预计 2026-11-05 11:13 完成 | 0（0） | 09:00→09:01 | `call_tool:search_materials` |
| 2 | S1 成本估算：预计合计 12 ≤ 预算 14，预计 2026-11-05 11:13 完成 ≤ 截止；下一步是 S2 素材检索 | `call_tool:search_materials` · mock | 上一步结果符合预期，按计划执行下一步 | `search_materials` | `{"topic": "双十一大促预热", "need": 3}` | 成功：找到 4 条相关素材 | 2（2） | 09:01→09:11 | `call_tool:generate_script` |
| 3 | S2 素材检索：找到 4 条相关素材；下一步是 S3 脚本生成（AI 分镜视频脚本） | `call_tool:generate_script` · mock | 上一步结果符合预期，按计划执行下一步 | `generate_script/premium` | `{"topic": "双十一大促预热", "material_ids": ["M05", "M07", "M12", "M06"]}` | 失败：[故障注入] 脚本生成（AI 分镜视频脚本）首次调用超时，可重试 | 2（4） | 09:11→11:11 | `retry` |
| 4 | S3 脚本生成（AI 分镜视频脚本）失败：TIMEOUT（可重试，[故障注入] 脚本生成（AI 分镜视频脚本）首次调用超时，可重试） | `retry` · mock | 临时错误，已尝试 1/2 次；重试后预计合计 14 ≤ 预算 14，预计 2026-11-05 13:13 完成 ≤ 截止 2026-11-05 18:00，先重试以保住原方案 | `generate_script/premium` | `{"topic": "双十一大促预热", "material_ids": ["M05", "M07", "M12", "M06"]}` | 成功：已生成AI 分镜视频脚本 | 10（14） | 11:11→13:11 | `call_tool:save_plan` |
| 5 | S3 脚本生成：已生成AI 分镜视频脚本；下一步是 S4 方案保存 | `call_tool:save_plan` · mock | 上一步结果符合预期，按计划执行下一步 | `save_plan` | `{"plan_id": "CP-43ea252c", "option_id": "P1"}` | 成功：方案已保存 | 0（14） | 13:11→13:13 | `结束` |
