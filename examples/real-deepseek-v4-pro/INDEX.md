# 真实模型运行记录索引

> 以下为真实模型 `llm:deepseek-v4-pro` 的实际输出，由 `python -m src.run_examples --policy llm --model deepseek-v4-pro` 生成。“决策依据”由模型填写。

| 场景 | 覆盖验收点 | 输入 | 实际输出 | 运行记录 | 状态 | 方案 | 费用/预算 | 工具调用 | 决策 |
|---|---|---|---|---|---|---|---|---|---|
| normal | 正常完成 | [normal.json](../inputs/normal.json) | [输出](outputs/normal.json) | [记录](traces/normal.md) | completed | P1 | 12 / 15 | 4 | 4 |
| over_budget | 预算超限（参考实例）：估算 12 > 预算 10 → 改选 → 再估算 8 → 保存 | [over_budget.json](../inputs/over_budget.json) | [输出](outputs/over_budget.json) | [记录](traces/over_budget.md) | completed | P2 | 8 / 10 | 5 | 5 |
| infeasible_budget | 预算超限且没有可行方案：返回未完成及缺口 | [infeasible_budget.json](../inputs/infeasible_budget.json) | [输出](outputs/infeasible_budget.json) | [记录](traces/infeasible_budget.md) | incomplete · NO_FEASIBLE_OPTION | P1 | 0 / 4 | 1 | 2 |
| tight_deadline | 截止时间不够：选能按时完成的方案 | [tight_deadline.json](../inputs/tight_deadline.json) | [输出](outputs/tight_deadline.json) | [记录](traces/tight_deadline.md) | completed | P3 | 5 / 15 | 4 | 4 |
| missing_budget | 缺预算：不调用工具，返回缺失项 | [missing_budget.json](../inputs/missing_budget.json) | [输出](outputs/missing_budget.json) | [记录](traces/missing_budget.md) | needs_input · MISSING_INPUT | — | 0 / None | 0 | 0 |
| missing_deadline | 缺截止时间：不调用工具，返回缺失项 | [missing_deadline.json](../inputs/missing_deadline.json) | [输出](outputs/missing_deadline.json) | [记录](traces/missing_deadline.md) | needs_input · MISSING_INPUT | — | 0 / 10 | 0 | 0 |
| no_material | 资料不足：素材库没有相关素材，如实返回未完成 | [no_material.json](../inputs/no_material.json) | [输出](outputs/no_material.json) | [记录](traces/no_material.md) | incomplete · NO_MATERIAL | P1 | 0 / 15 | 2 | 3 |
| partial_materials | 部分成功：素材不够旗舰方案，改选并复用检索结果 | [partial_materials.json](../inputs/partial_materials.json) | [输出](outputs/partial_materials.json) | [记录](traces/partial_materials.md) | completed | P2 | 8 / 15 | 5 | 5 |
| tool_failure | 工具失败（不可重试）：调整计划，保留成功结果 | [tool_failure.json](../inputs/tool_failure.json) | [输出](outputs/tool_failure.json) | [记录](traces/tool_failure.md) | completed | P2 | 10 / 15 | 6 | 6 |
| tool_timeout_retry | 工具失败（可重试）：重试后预算内完成 | [tool_timeout_retry.json](../inputs/tool_timeout_retry.json) | [输出](outputs/tool_timeout_retry.json) | [记录](traces/tool_timeout_retry.md) | completed | P1 | 14 / 14 | 5 | 5 |
| search_down | 工具失败且无替代：停止，失败步骤不记为成功 | [search_down.json](../inputs/search_down.json) | [输出](outputs/search_down.json) | [记录](traces/search_down.md) | incomplete · TOOL_UNAVAILABLE | P1 | 0 / 16 | 2 | 3 |
| repeat.run1 | 重复执行：第二次直接返回，不调用工具、不重复计费 | [repeat.json](../inputs/repeat.json) | [输出](outputs/repeat.run1.json) | [记录](traces/repeat.run1.md) | completed | P1 | 12 / 18 | 4 | 4 |
| repeat.run2 | 重复执行：第二次直接返回，不调用工具、不重复计费 | [repeat.json](../inputs/repeat.json) | [输出](outputs/repeat.run2.json) | [记录](traces/repeat.run2.md) | completed · 重复执行 | P1 | 12 / 18 | 4 | 4 |
| resume.run1 | 失败步骤续跑（加分项）：保存失败后再次提交，只补跑失败步骤 | [resume_fail.json](../inputs/resume_fail.json) | [输出](outputs/resume.run1.json) | [记录](traces/resume.run1.md) | incomplete · TOOL_UNAVAILABLE | P1 | 12 / 20 | 4 | 5 |
| resume.run2 | 失败步骤续跑（加分项）：保存失败后再次提交，只补跑失败步骤 | [resume_ok.json](../inputs/resume_ok.json) | [输出](outputs/resume.run2.json) | [记录](traces/resume.run2.md) | completed | P1 | 12 / 20 | 6 | 7 |
| tool_limit | 工具调用上限：达到上限返回明确的未完成状态 | [tool_limit.json](../inputs/tool_limit.json) | [输出](outputs/tool_limit.json) | [记录](traces/tool_limit.md) | incomplete · TOOL_CALL_LIMIT | P1 | 12 / 17 | 3 | 4 |
