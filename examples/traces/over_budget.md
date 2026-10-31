# 运行记录：over_budget

> 离线 Mock：决策由固定规则产生，只验证流程，不证明真实模型的决策效果

- 决策模块：`mock`
- 结果状态：**completed**
- 费用合计：8 / 预算 10
- 用量：{"tool_calls": 5, "model_turns": 5, "max_tool_calls": 15, "max_model_turns": 30}
- 已保存方案：CP-c1802d79（版本 1，created）

## 执行轨迹

| 步骤 | 触发（观察） | 动作 · 决策者 | 决策依据 | 工具 | 参数 | 返回摘要 | 费用（累计） | 模拟时钟 | 后续动作 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 初始规划 | `plan:P1` · mock | 选择效果优先的 P1 旗舰方案：AI 分镜短视频，先调用成本估算确认预算与截止时间 | `estimate_cost` | `{"option_id": "P1", "spent": 0, "clock": "2026-11-02 09:00", "items": ["estimate_cost", "search_materials", "generate_script:premium", "save_plan"]}` | 成功：P1 预计合计 12，预计 2026-11-02 11:13 完成 | 0（0） | 09:00→09:01 | `switch_option:P2` |
| 2 | 估算 P1：预计合计 12 > 预算 10（超 2） | `switch_option:P2` · mock | 改选 P2 精简方案：标准短视频：按费用表预计合计 8 ≤ 预算 10，预计 2026-11-02 10:14 完成 ≤ 截止 2026-11-02 18:00，是可行候选中效果最好的；已成功步骤直接复用 | `estimate_cost` | `{"option_id": "P2", "spent": 0, "clock": "2026-11-02 09:01", "items": ["estimate_cost", "search_materials", "generate_script:standard", "save_plan"]}` | 成功：P2 预计合计 8，预计 2026-11-02 10:14 完成 | 0（0） | 09:01→09:02 | `call_tool:search_materials` |
| 3 | S5 成本估算：预计合计 8 ≤ 预算 10，预计 2026-11-02 10:14 完成 ≤ 截止；下一步是 S6 素材检索 | `call_tool:search_materials` · mock | 上一步结果符合预期，按计划执行下一步 | `search_materials` | `{"topic": "秋季新品上市", "need": 2}` | 成功：找到 4 条相关素材 | 2（2） | 09:02→09:12 | `call_tool:generate_script` |
| 4 | S6 素材检索：找到 4 条相关素材；下一步是 S7 脚本生成（标准短视频脚本） | `call_tool:generate_script` · mock | 上一步结果符合预期，按计划执行下一步 | `generate_script/standard` | `{"topic": "秋季新品上市", "material_ids": ["M01", "M02", "M03", "M04"]}` | 成功：已生成标准短视频脚本 | 6（8） | 09:12→10:12 | `call_tool:save_plan` |
| 5 | S7 脚本生成：已生成标准短视频脚本；下一步是 S8 方案保存 | `call_tool:save_plan` · mock | 上一步结果符合预期，按计划执行下一步 | `save_plan` | `{"plan_id": "CP-c1802d79", "option_id": "P2"}` | 成功：方案已保存 | 0（8） | 10:12→10:14 | `结束` |
