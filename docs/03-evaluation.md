# 03 评估：测试预期、实际结果、失败与限制

## 验证范围

| 验证项 | 状态 | 证据 |
|---|---|---|
| 工具、状态机、预算、幂等、续跑、上限 | ✅ 确定性测试 | [test-results.txt](../examples/test-results.txt) |
| 全部验收场景端到端（离线 Mock） | ✅ 14 个场景 / 16 次运行 | [examples/README.md](../examples/README.md) |
| **真实模型决策（DeepSeek）** | ✅ `deepseek-v4-pro`、`deepseek-flash` 各跑全部 14 个场景 / 16 次运行 | [v4-pro 索引](../examples/real-deepseek-v4-pro/INDEX.md)、[flash 索引](../examples/real-deepseek-flash/INDEX.md) |
| 随机决策下的硬约束 | ✅ 600 个随机请求（固定种子），每个带随机故障重复提交 3 次，决策模块随机选动作或返回垃圾输出 | `tests/test_fuzz.py` |
| 已提交运行记录的不变量 | ✅ 每次跑测试都逐份检查全部记录 | `tests/test_records.py` |
| 提交的 Mock 记录就是当前代码的实际输出 | ✅ 测试时重跑并逐字段比对 | `tests/test_records.py` |
| 真实模型的统计稳定性 | ⚠️ 每个模型只跑了最终代码的一轮全量 | 见「失败与限制」 |

## 场景结果

“Mock” 列可以完全复现；两个真实模型列是最终代码的实际运行结果。每格为：方案 · 费用合计 / 预算。

| 场景 | 验收点 | 预期 | deepseek-v4-pro | deepseek-flash | 离线 Mock |
|---|---|---|---|---|---|
| [normal](../examples/inputs/normal.json) | 正常完成 | P1 · 12 / 15，已保存 | P1 · 12 / 15 | P1 · 12 / 15 | P1 · 12 / 15 |
| [over_budget](../examples/inputs/over_budget.json) | 预算超限（参考实例） | 估算 12 → 改选 P2 → 再估算 8 → 保存 | P2 · 8 / 10 | P2 · 8 / 10 | P2 · 8 / 10 |
| [infeasible_budget](../examples/inputs/infeasible_budget.json) | 没有可行方案 | 未完成，花费 0，缺口“P3 预算还差 1” | 未完成 · 0 / 4 | 未完成 · 0 / 4 | 未完成 · 0 / 4 |
| [tight_deadline](../examples/inputs/tight_deadline.json) | 截止时间不够 | 直接选来得及的 P3 | P3 · 5 / 15 | P3 · 5 / 15 | P3 · 5 / 15 |
| [missing_budget](../examples/inputs/missing_budget.json) | 缺预算 | `needs_input`，不调用工具 | 同预期 | 同预期 | 同预期 |
| [missing_deadline](../examples/inputs/missing_deadline.json) | 缺截止时间 | `needs_input`，不调用工具 | 同预期 | 同预期 | 同预期 |
| [no_material](../examples/inputs/no_material.json) | 资料不足 | 未完成，不编造，`NO_MATERIAL` | 未完成 · 0 / 15 | 未完成 · 0 / 15 | 未完成 · 0 / 15 |
| [partial_materials](../examples/inputs/partial_materials.json) | 部分成功 | 2 条 < 3 条 → 改选 P2，复用检索 | P2 · 8 / 15 | P2 · 8 / 15 | P2 · 8 / 15 |
| [tool_failure](../examples/inputs/tool_failure.json) | 工具失败，换方案 | AI 分镜失败 → 改选 P2，检索不重跑 | P2 · 10 / 15 | P2 · 10 / 15 | P2 · 10 / 15 |
| [tool_timeout_retry](../examples/inputs/tool_timeout_retry.json) | 工具失败，重试 | 超时 → 重试，合计恰好等于预算 | P1 · 14 / 14 | P1 · 14 / 14 | P1 · 14 / 14 |
| [search_down](../examples/inputs/search_down.json) | 工具失败且无替代 | 未完成，失败步骤不记为成功 | 未完成 · 0 / 16 | 未完成 · 0 / 16 | 未完成 · 0 / 16 |
| repeat（[第 1、2 次](../examples/inputs/repeat.json)） | 重复执行 | 第 2 次 `repeat=true`，调用数不变 | P1 · 12 / 18，调用 4 → 4 | P1 · 12 / 18，调用 4 → 4 | P1 · 12 / 18，调用 4 → 4 |
| resume（[失败](../examples/inputs/resume_fail.json) → [恢复](../examples/inputs/resume_ok.json)） | 失败步骤续跑 | 第 2 次只调用估算和保存，费用不变 | 未完成 → 完成，12 / 20 | 未完成 → 完成，12 / 20 | 未完成 → 完成，12 / 20 |
| [tool_limit](../examples/inputs/tool_limit.json) | 工具调用上限 | 3 次后 `TOOL_CALL_LIMIT`，未保存 | 未完成 · 12 / 17 | 未完成 · 12 / 17 | 未完成 · 12 / 17 |

费用可以手工核算。以 tool_failure 为例：检索 2 + AI 分镜失败按 `fee_on_failure` 收 2 + 标准脚本 6 = 10。

## 模型根据不同工具结果调整动作（试卷要求的运行记录）

以下引号里是 `deepseek-v4-pro` 在记录中自己填写的决策依据。记录里每一步都有工具参数、返回摘要、决策依据和后续动作，并保存了接口返回的响应 ID。

**有多个可选动作时，模型作出的选择：**

1. **估算超预算 → 改选方案**（[over_budget](../examples/real-deepseek-v4-pro/traces/over_budget.md)）：`estimate_cost` 返回 P1 预计合计 12 > 预算 10。模型的依据是“P1超预算2单位不可行，选择可行且优先级更高的P2（预计费用8，预算10内）”，再次估算得 8 后继续。P1 的付费步骤一次都没有执行。
2. **同样是脚本工具失败，错误不同 → 动作不同：**
   - 不可重试（[tool_failure](../examples/real-deepseek-v4-pro/traces/tool_failure.md)）：“P1 的 premium 脚本生成工具不可用且不可重试，当前已不可行；剩余预算和时间内 P2 精简方案可行且优先级最高，总费用10、预计12:14完成，均满足约束。”检索结果直接复用。
   - 可重试（[tool_timeout_retry](../examples/real-deepseek-v4-pro/traces/tool_timeout_retry.md)）：“S3 脚本生成超时但可重试，P1 当前可行：预计总费用14未超预算14，预计13:13完成早于18:00，优先保住旗舰方案。”
3. **素材部分成功 → 改选够用的方案**（[partial_materials](../examples/real-deepseek-v4-pro/traces/partial_materials.md)）：“P1 需要 3 条素材但仅找到 2 条，已不可行；P2 优先级更高且可行，预估费用 8 未超预算 15，预估完成 10:14 早于截止 18:00。”这次检索的结果继续使用。
4. **保存失败后续跑**（[第 1 次](../examples/real-deepseek-v4-pro/traces/resume.run1.md)、[第 2 次](../examples/real-deepseek-v4-pro/traces/resume.run2.md)）：第 1 次保存失败，检索和脚本结果保留。第 2 次模型选择继续 P1：“当前旗舰方案P1的S1-S3已成功，仅S4方案保存失败；已花费12/预算20，截止时间尚有407分钟，预算和时间仍允许继续，选择P1保住效果最好的方案并继续保存。”只补跑估算和保存，费用不变。

**没有其他可行动作时，执行器只提供 `stop`，模型的作用是说明缺口，不算模型的选择：**

5. **没有可行方案**（[infeasible_budget](../examples/real-deepseek-v4-pro/traces/infeasible_budget.md)）：“所有候选方案预计费用均超出预算4（P1超8、P2超4、P3超1），且当前允许动作只剩stop，无法在预算内完成任何方案。”只花了一次免费估算。
6. **工具失败且没有替代**（[search_down](../examples/real-deepseek-v4-pro/traces/search_down.md)）：所有方案都依赖素材检索，失败步骤如实记为失败，后续步骤标为未执行。

## 真实模型发现的问题

这两个问题只有用真实模型才暴露得出来，当时 Mock 和全部测试都是通过的。

1. **发给模型的状态有误，模型主动降级。** 第一版把当前方案标成 `feasible=false, blocked_by=当前方案`（本意是不让它出现在改选列表里）。`deepseek-flash` 如实读成“当前方案不可行”，在估算通过后主动改选 P2，导致 `tool_failure` 等场景根本没有碰到注入的故障，记录也就证明不了验收点。修复：候选方案如实标出 `is_current` 和 `feasible`，并在提示词里写明目标是预算内效果最好。回归保护：`test_model_is_told_the_truth_about_the_current_option`，以及要求每份真实记录都确实碰到故障的 `test_real_records_actually_exercise_each_scenario`。
2. **模型输出多余内容，被误判为服务故障。** `deepseek-flash` 有一次在 JSON 之后多输出了内容，解析失败后触发熔断，本轮后面的决策全部走了 Mock。修复：只取第一个 JSON 对象；格式错误退回给模型重选，不再熔断。回归保护：`test_model_output_with_extra_text_is_parsed`、`test_unparsable_model_output_is_returned_to_the_model_not_treated_as_an_outage`。

## 自动化测试

运行 `python -m unittest discover -s tests -v`。实际输出见 [test-results.txt](../examples/test-results.txt)。

| 文件 | 覆盖 |
|---|---|
| `tests/test_acceptance.py` | 全部验收点；预算与截止时间边界；缺口说明；重试与改选；部分成功；幂等；续跑；进程中断恢复；工具调用和决策次数上限；决策校验、退回重选、回退 Mock、熔断；发给模型的状态；密钥不泄露；输入校验、退出码、BOM/UTF-16 输入；“预算 × 故障”组合网格 |
| `tests/test_fuzz.py` | 决策模块随机选动作或返回垃圾输出时，硬约束仍然成立；已完成的请求不再调用工具；保存的方案与最终方案一致 |
| `tests/test_records.py` | 全部记录的不变量；真实记录标注的决策来源是模型、没有回退 Mock、每次决策都有接口响应 ID、确实碰到了注入的故障；提交的 Mock 记录与当前代码的输出一致 |
| `tests/invariants.py` | 每次运行和每份记录都要过的检查：费用合计 = 账本之和；任何时刻累计 ≤ 预算；时钟连续；依赖先于步骤；状态与最后一次调用结果一致；尝试次数 = 调用记录数；每次工具调用之前都有决策选中它 |

## 失败与限制

1. **真实模型的稳定性没有统计。** 最终代码上每个模型只跑了一轮全量。两个模型在 14 个场景上的动作序列一致，但样本太少，不能说明决策一定稳定。
2. **真实模型的动作和 Mock 完全一致。** 每个场景都只有一个明显合理的答案，提示词也写明了优先顺序，所以三者走的是同一条路。这些记录能证明模型读懂了工具结果并作出了对应选择，证据是模型自己写的依据、接口响应 ID，以及那些存在其他可选动作的决策（每个模型 52 次决策里有 42 次）；不能证明模型在有争议的取舍上更好。
3. **模型的决策空间有限。** 模型选择动作，工具参数由执行器按计划填入，也不能改变工具的先后顺序；有其他可行动作时不能选择停止；是否完成由执行器判定。这是有意的取舍，理由见 [02 方案](02-solution.md)。
4. **工具都是合成的。** 费用和耗时来自模拟表，脚本由模板生成，不代表真实服务的质量和成本。
5. **并发提交没有加锁。** 同一请求并行执行时，工具可能被调用两次，Windows 上还可能因文件占用报错。已列入下一步。
6. **保存的崩溃恢复不完整。** 进程恰好在保存工具写入方案后、调用结果落盘前中断时，结果未知：续跑会再保存一次，方案仍只有一份，但版本号 +1；如果这时已经来不及重新估算，会返回未完成，而方案其实已写入方案库。
7. **工具恢复后不会自动回到更好的方案。** 续跑只重新允许导致停止的那一步，其他已判定不可用的工具不自动重试；要全部重来用 `--reset`。
8. **预算或截止时间变更算新请求。** 已保留的步骤带不过去。
9. **续跑沿用已保存的模拟时钟**，不按真实时间推进。
