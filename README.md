# 内容任务规划 Agent（项目题 2）

帮运营人员在**预算**和**截止时间**内完成一份内容活动方案。

Agent 由模型驱动：模型选择动作 → 执行工具 → 观察结果 → 调整下一步。遇到超预算、超截止时间、工具错误或部分成功时，模型决定重试还是改选替代方案；没有可行动作时停止，并说明缺口。已成功步骤的结果保留复用。全部状态都会落盘，所以重复提交是幂等的，失败的步骤也可以续跑。

费用合计和步骤状态都以执行器的账本和实际调用结果为准，不采信模型的自述。是否完成由执行器判定：全部步骤满足才算完成。

| 项目 | 内容 |
|---|---|
| 题号 | 2 · 内容任务规划 Agent |
| GitHub URL / commit SHA | https://github.com/luc1ferxx/content-plan-agent ；commit SHA 以提交表单为准（`main` 分支最新提交；README 无法写入自身所在提交的 SHA） |
| 运行方式 | CLI（Python 3.10+，无第三方依赖） |
| 模型 | DeepSeek（OpenAI 兼容接口，用标准库调用），默认 `deepseek-v4-pro`，也可用 `deepseek-flash`；没有 Key 时用离线 Mock |
| 真实模型运行记录 | 两个模型各跑全部 14 个场景：[real-deepseek-v4-pro](examples/real-deepseek-v4-pro/INDEX.md)、[real-deepseek-flash](examples/real-deepseek-flash/INDEX.md) |
| 实际用时 / 未完成部分 | 用时见 [04 AI 使用](docs/04-ai-usage.md)。未做：并发提交同一请求时的加锁、预算变更后沿用已有结果（见 [03 评估](docs/03-evaluation.md)「限制」） |

> 边界说明：`examples/outputs`、`examples/traces` 是**离线 Mock**（固定规则模拟模型）的输出，只验证流程，不证明模型决策效果；真实模型的决策证据在 `examples/real-*` 目录。

## 环境与安装

需要 Python 3.10 或更高版本。没有第三方依赖，`requirements.txt` 只是声明这一点，不用安装任何东西。

## 快速开始（无需密钥）

在项目根目录执行。

跑全部测试：

```bash
python -m unittest discover -s tests -v
```

用离线 Mock 跑题目的参考实例（预算 10：估算 12 → 改选 → 再估算 8 → 保存）：

```bash
python -m src.main --input examples/inputs/over_budget.json
```

重新生成全部 Mock 样例：

```bash
python -m src.run_examples
```

Mock 只验证流程。每份 Mock 输出都带有边界说明，`mode` 字段为 `offline_mock`。

## 用真实模型运行

复制 `.env.example` 为 `.env`，填入 `DEEPSEEK_API_KEY`，然后加上 `--policy llm`：

```bash
python -m src.main --input examples/inputs/over_budget.json --policy llm
```

重新生成某个模型的全部运行记录（会覆盖 `examples/real-<模型名>/`）：

```bash
python -m src.run_examples --policy llm --model deepseek-flash
```

没有配置 Key 时，`--policy llm` 会返回退出码 2 并提示配置，不会悄悄改用 Mock。模型调用中途失败时，本轮改用 Mock 继续，决策记录里写明原因，整次运行的 `mode` 标为 `real_model_with_mock_fallback` 并带上边界说明。

## 评审入口

| 检查项 | 入口 |
|---|---|
| 业务目标与验收标准 | [01 需求](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [02 方案](docs/02-solution.md) |
| 测试预期、实际结果、失败与限制 | [03 评估](docs/03-evaluation.md) |
| AI 辅助内容、个人修改及验证 | [04 AI 使用](docs/04-ai-usage.md) |
| 最难的问题、学习收获、下一步 | [05 复盘](docs/05-retrospective.md) |
| 全部场景的输入、输出、运行记录 | [Mock 索引](examples/README.md)、[v4-pro 索引](examples/real-deepseek-v4-pro/INDEX.md)、[flash 索引](examples/real-deepseek-flash/INDEX.md) |
| 实际测试输出 | [test-results.txt](examples/test-results.txt) |

## 验收场景

命令前缀都是 `python -m src.main --input`。以下结果是离线 Mock 的，可以完全复现；真实模型的结果见 [03 评估](docs/03-evaluation.md)。

| 验收项 | 输入 | 预期结果 |
|---|---|---|
| 正常完成 | `examples/inputs/normal.json` | 估算 P1 = 12 ≤ 15 → 检索 → AI 分镜脚本 → 保存；费用合计 12，退出码 0 |
| 预算超限（参考实例） | `examples/inputs/over_budget.json` | 估算 P1 = 12 > 10 → 改选 P2 → 再估算 8 → 检索 → 标准脚本 → 保存；费用合计 8 |
| 预算超限，且没有可行方案 | `examples/inputs/infeasible_budget.json` | 只调用 1 次估算，花费 0；返回未完成和缺口：“最接近可行的是 P3 …：预算还差 1（时间充足）” |
| 截止时间不够 | `examples/inputs/tight_deadline.json` | P1、P2 按费用表都来不及，直接选 P3，09:33 完成 |
| 缺预算 / 缺截止时间 | `examples/inputs/missing_budget.json`、`examples/inputs/missing_deadline.json` | `needs_input`，列出缺失字段，不调用工具，费用 0 |
| 资料不足 | `examples/inputs/no_material.json` | 素材库没有相关素材，返回未完成（`NO_MATERIAL`），不编造 |
| 部分成功 | `examples/inputs/partial_materials.json` | 只检索到 2 条 < P1 需要的 3 条，步骤记为“部分成功”；改选 P2 并复用这次检索结果 |
| 工具失败 → 换方案 | `examples/inputs/tool_failure.json` | 检索成功并保留；AI 分镜不可用，如实记为失败（计 2）；改选 P2 并再估算后完成，费用 2 + 2 + 6 = 10 |
| 工具失败 → 重试 | `examples/inputs/tool_timeout_retry.json` | 首次超时；重试后合计 14 ≤ 预算 14，重试成功；费用 2 + 2 + 10 = 14 |
| 工具失败且无替代 | `examples/inputs/search_down.json` | 花费 0，检索记为失败，后续步骤未执行，返回未完成 |
| 工具调用上限 | `examples/inputs/tool_limit.json` | 调用 3 次后停止，`stop_code` 为 `TOOL_CALL_LIMIT`，超限的调用在决策记录里留痕 |
| 重复执行 | 把任一已完成的命令再跑一次 | 直接返回已保存的方案，不调用工具、不重复计费，方案库里仍只有 1 份 |
| 失败步骤续跑（加分项） | ① `examples/inputs/resume_fail.json`<br>② `examples/inputs/resume_ok.json` | ① 保存失败，检索和脚本的结果保留，状态未完成；② 只重新估算并保存，费用仍是 12，显示“已续跑 1 次” |

退出码：0 = 已完成，1 = 未完成或缺少输入，2 = 输入格式错误或未配置模型，3 = 运行异常。

## 注入一次失败

对任意**尚未执行过**的请求加上 `--fault 工具[:模式]=类型`，例如：

```bash
python -m src.main --input examples/inputs/normal.json --budget 19 --fault generate_script:premium=fail_always
```

| 类型 | 效果 |
|---|---|
| `fail_once` | 首次调用超时（TIMEOUT，可重试）。“首次”按持久化的调用历史判断，续跑后不会再次触发 |
| `fail_always` | 工具不可用（不可重试） |
| `partial` | 只用于素材检索：返回的条数少于方案需要的条数（方案只需要 1 条时不会形成部分成功） |

注入目标可以是工具（`save_plan`），也可以是工具的某个模式（`generate_script:premium`）。

同一请求（主题 + 预算 + 截止时间相同）完成后再提交，会按幂等规则直接返回已有结果。要看到故障效果，需要换个参数、加 `--reset`，或换一个 `--state-dir`。

运行状态与方案库默认放在 `.runtime/`（已被 gitignore）。其他常用参数：`--json` 输出完整结果（含逐步 `trace`），`--output 文件` 以 UTF-8 写入文件，`--max-tool-calls`、`--max-model-turns` 调整上限。输入文件支持 UTF-8（含 BOM）和 UTF-16。

## 目录结构

```text
.
├── README.md
├── requirements.txt         # 声明无第三方依赖
├── .env.example             # 真实模型所需变量；Mock 和测试不需要
├── data/                    # 工具费用与耗时表、方案模板、合成素材
├── docs/                    # 01 需求、02 方案、03 评估、04 AI 使用、05 复盘
├── examples/
│   ├── README.md            # 场景索引（Mock）
│   ├── inputs/              # 各场景的输入
│   ├── outputs/、traces/    # 离线 Mock 的实际输出与运行记录
│   ├── real-<模型名>/       # 真实模型的实际输出与运行记录
│   └── test-results.txt     # 实际测试输出
├── src/
│   ├── main.py              # CLI 入口
│   ├── agent.py             # 执行循环：计划、执行、观察、决策校验、重规划、续跑
│   ├── policy.py            # 允许动作、决策校验、离线 Mock
│   ├── llm.py               # DeepSeek 决策
│   ├── tools.py             # 4 个工具与故障注入
│   ├── catalog.py           # 费用表、方案模板、素材、估算算法
│   ├── common.py            # 路径、JSON 读写、模拟时钟、.env 读取
│   ├── state.py             # 运行状态库、方案库
│   ├── request.py           # 输入校验与幂等键
│   ├── trace.py             # 运行记录
│   ├── report.py            # 中文文本报告
│   └── run_examples.py      # 批量生成样例
└── tests/
    ├── invariants.py        # 预算、依赖、状态、决策的不变量
    ├── test_acceptance.py   # 验收与循环测试
    ├── test_fuzz.py         # 随机决策测试（固定种子）
    └── test_records.py      # 对提交的运行记录逐份自检
```

## 已知限制

- 工具和素材都是合成的，脚本生成用模板输出。
- 模型只选择动作，工具参数由执行器按计划填入；有其他可行动作时不能选择停止。
- 并发提交同一请求时没有加锁，请勿并行执行。
- 预算或截止时间变了就算新请求，已保留的步骤不会带过去。
- 进程恰好在保存工具写入方案后、调用结果落盘前中断时，续跑会再保存一次，方案版本号 +1（方案仍只有一份）；如果这时已经来不及重新估算，会返回未完成，但方案已写入方案库。
- 续跑沿用已保存的模拟时钟，不按真实时间推进。
