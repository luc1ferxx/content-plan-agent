# 运行记录：tight_deadline

> 离线 Mock：决策由固定规则产生，只验证流程，不证明真实模型的决策效果

- 决策模块：`mock`
- 结果状态：**completed**
- 费用合计：5 / 预算 15
- 用量：{"tool_calls": 4, "model_turns": 4, "max_tool_calls": 15, "max_model_turns": 30}
- 已保存方案：CP-2d6bc8a1（版本 1，created）

## 执行轨迹

| 步骤 | 触发（观察） | 动作 · 决策者 | 决策依据 | 工具 | 参数 | 返回摘要 | 费用（累计） | 模拟时钟 | 后续动作 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 初始规划 | `plan:P3` · mock | 选择效果优先的 P3 保底方案：图文推文，先调用成本估算确认预算与截止时间；跳过 P1（按费用表预计 2026-11-02 11:13 完成，已晚于截止）、P2（按费用表预计 2026-11-02 10:13 完成，已晚于截止） | `estimate_cost` | `{"option_id": "P3", "spent": 0, "clock": "2026-11-02 09:00", "items": ["estimate_cost", "search_materials", "generate_script:lite", "save_plan"]}` | 成功：P3 预计合计 5，预计 2026-11-02 09:33 完成 | 0（0） | 09:00→09:01 | `call_tool:search_materials` |
| 2 | S1 成本估算：预计合计 5 ≤ 预算 15，预计 2026-11-02 09:33 完成 ≤ 截止；下一步是 S2 素材检索 | `call_tool:search_materials` · mock | 上一步结果符合预期，按计划执行下一步 | `search_materials` | `{"topic": "秋季新品上市", "need": 1}` | 成功：找到 4 条相关素材 | 2（2） | 09:01→09:11 | `call_tool:generate_script` |
| 3 | S2 素材检索：找到 4 条相关素材；下一步是 S3 脚本生成（图文推文脚本） | `call_tool:generate_script` · mock | 上一步结果符合预期，按计划执行下一步 | `generate_script/lite` | `{"topic": "秋季新品上市", "material_ids": ["M01", "M02", "M03", "M04"]}` | 成功：已生成图文推文脚本 | 3（5） | 09:11→09:31 | `call_tool:save_plan` |
| 4 | S3 脚本生成：已生成图文推文脚本；下一步是 S4 方案保存 | `call_tool:save_plan` · mock | 上一步结果符合预期，按计划执行下一步 | `save_plan` | `{"plan_id": "CP-2d6bc8a1", "option_id": "P3"}` | 成功：方案已保存 | 0（5） | 09:31→09:33 | `结束` |
