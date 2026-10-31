"""批量运行 examples/inputs 下的验收场景，把实际输出与运行记录写入 examples/。

离线 Mock：python -m src.run_examples                      → examples/outputs、examples/traces、examples/README.md
真实模型：python -m src.run_examples --policy llm [--model deepseek-flash] → examples/real-<模型名>/
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from .agent import PlannerAgent
from .catalog import Catalog
from .common import ROOT, load_dotenv, load_json
from .main import _setup_streams, make_policy
from .request import normalize_request
from .state import PlanStore, RunStore
from .trace import render_trace

# (场景, 覆盖的验收点, 依次运行的输入文件；同一场景共用一份运行状态)
SCENARIOS = [
    ("normal", "正常完成", ["normal.json"]),
    ("over_budget", "预算超限（参考实例）：估算 12 > 预算 10 → 改选 → 再估算 8 → 保存", ["over_budget.json"]),
    ("infeasible_budget", "预算超限且没有可行方案：返回未完成及缺口", ["infeasible_budget.json"]),
    ("tight_deadline", "截止时间不够：选能按时完成的方案", ["tight_deadline.json"]),
    ("missing_budget", "缺预算：不调用工具，返回缺失项", ["missing_budget.json"]),
    ("missing_deadline", "缺截止时间：不调用工具，返回缺失项", ["missing_deadline.json"]),
    ("no_material", "资料不足：素材库没有相关素材，如实返回未完成", ["no_material.json"]),
    ("partial_materials", "部分成功：素材不够旗舰方案，改选并复用检索结果", ["partial_materials.json"]),
    ("tool_failure", "工具失败（不可重试）：调整计划，保留成功结果", ["tool_failure.json"]),
    ("tool_timeout_retry", "工具失败（可重试）：重试后预算内完成", ["tool_timeout_retry.json"]),
    ("search_down", "工具失败且无替代：停止，失败步骤不记为成功", ["search_down.json"]),
    ("repeat", "重复执行：第二次直接返回，不调用工具、不重复计费", ["repeat.json", "repeat.json"]),
    ("resume", "失败步骤续跑（加分项）：保存失败后再次提交，只补跑失败步骤", ["resume_fail.json", "resume_ok.json"]),
    ("tool_limit", "工具调用上限：达到上限返回明确的未完成状态", ["tool_limit.json"]),
]
INPUTS = ROOT / "examples" / "inputs"


def run_scenario(name: str, files: list[str], catalog: Catalog, policy_factory) -> list[tuple[str, str, dict]]:
    with tempfile.TemporaryDirectory() as tmp:
        results = []
        for i, file in enumerate(files, 1):
            label = f"{name}.run{i}" if len(files) > 1 else name
            req = normalize_request(load_json(INPUTS / file), catalog.fault_keys())
            agent = PlannerAgent(catalog, RunStore(Path(tmp)), PlanStore(Path(tmp)), policy_factory())
            results.append((label, file, agent.run(req)))
        return results


def write_records(out_dir: Path, results: list, covers: dict) -> None:
    (out_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    for label, _, result in results:
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        (out_dir / "outputs" / f"{label}.json").write_text(text + "\n", encoding="utf-8", newline="\n")
        (out_dir / "traces" / f"{label}.md").write_text(render_trace(label, result), encoding="utf-8", newline="\n")


def write_index(path: Path, results: list, covers: dict, title: str, note: str, input_prefix: str, prefix: str = "") -> None:
    lines = [f"# {title}", "", f"> {note}", "",
             "| 场景 | 覆盖验收点 | 输入 | 实际输出 | 运行记录 | 状态 | 方案 | 费用/预算 | 工具调用 | 决策 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for label, file, r in results:
        option = r["plan"]["option_id"] if r["plan"] else "—"
        status = r["status"] + (f" · {r['stop_code']}" if r["stop_code"] else "") + (" · 重复执行" if r["repeat"] else "")
        lines.append(f"| {label} | {covers[label]} | [{file}]({input_prefix}{file}) | "
                     f"[输出]({prefix}outputs/{label}.json) | [记录]({prefix}traces/{label}.md) | {status} | {option} | "
                     f"{r['cost']['spent']} / {r['cost']['budget']} | {r['usage']['tool_calls']} | {r['usage']['model_turns']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    _setup_streams()
    parser = argparse.ArgumentParser(prog="python -m src.run_examples")
    parser.add_argument("--policy", choices=("mock", "llm"), default="mock")
    parser.add_argument("--model", help="真实模型名")
    parser.add_argument("--only", help="只运行这些场景，逗号分隔")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    catalog = Catalog()
    try:
        name = make_policy(args.policy, args.model).name
    except ValueError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2
    real = args.policy == "llm"
    out_dir = ROOT / "examples" / f"real-{name.split(':', 1)[1]}" if real else ROOT / "examples"
    only = set(args.only.split(",")) if args.only else None
    if not only:  # 全量运行时清掉旧记录，避免留下过期文件
        for sub in ("outputs", "traces"):
            shutil.rmtree(out_dir / sub, ignore_errors=True)
    results, covers = [], {}
    for scenario, cover, files in SCENARIOS:
        if only and scenario not in only:
            continue
        for label, file, result in run_scenario(scenario, files, catalog, lambda: make_policy(args.policy, args.model)):
            covers[label] = cover
            results.append((label, file, result))
            code = f" · {result['stop_code']}" if result["stop_code"] else ""
            option = result["plan"]["option_id"] if result["plan"] else "—"
            print(f"{label}: {result['status']}{code} · {option} · 费用 {result['cost']['spent']}/{result['cost']['budget']}", flush=True)
    write_records(out_dir, results, covers)
    # 索引总是按目录里现有的全部记录重建，这样只补跑个别场景时也保持一致
    recorded = []
    for scenario, cover, files in SCENARIOS:
        for i, file in enumerate(files, 1):
            label = f"{scenario}.run{i}" if len(files) > 1 else scenario
            path = out_dir / "outputs" / f"{label}.json"
            if path.exists():
                covers[label] = cover
                recorded.append((label, file, load_json(path)))
    if real:
        model = name.split(":", 1)[1]
        write_index(out_dir / "INDEX.md", recorded, covers, "真实模型运行记录索引",
                    f"以下为真实模型 `{name}` 的实际输出，由 `python -m src.run_examples --policy llm --model {model}` 生成。“决策依据”由模型填写。",
                    "../inputs/")
    else:
        write_index(out_dir / "README.md", recorded, covers, "运行样例索引（离线 Mock）",
                    "以下为离线 Mock 的实际输出，由 `python -m src.run_examples` 生成：决策由固定规则产生，只验证流程，不证明真实模型的决策效果。真实模型的决策证据在 `real-*` 目录。",
                    "inputs/")
    print(f"已写入 {out_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
