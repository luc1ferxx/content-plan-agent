"""命令行入口。退出码：0=已完成；1=未完成或缺少必要输入；2=输入格式错误或未配置模型；3=运行异常。

离线 Mock：python -m src.main --input examples/inputs/over_budget.json
真实模型：python -m src.main --input examples/inputs/over_budget.json --policy llm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import PlannerAgent
from .catalog import Catalog
from .common import ROOT, STATE_DIR, load_dotenv, load_json
from .llm import DeepSeekPolicy
from .policy import MockPolicy
from .report import render
from .request import normalize_request
from .state import PlanStore, RunStore


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.main", description="内容任务规划 Agent：在预算与截止时间内产出内容活动方案")
    p.add_argument("--input", help="请求 JSON 文件（topic / budget / deadline / start_time / simulate.faults / limits）")
    p.add_argument("--topic", help="活动主题（覆盖 JSON）")
    p.add_argument("--budget", type=float, help="预算，模拟单位（覆盖 JSON）")
    p.add_argument("--deadline", help="截止时间 'YYYY-MM-DD HH:MM'（覆盖 JSON）")
    p.add_argument("--start", help="模拟开始时间 'YYYY-MM-DD HH:MM'，默认当前时间（覆盖 JSON）")
    p.add_argument("--fault", action="append", default=[], metavar="TOOL[:MODE]=KIND",
                   help="故障注入，可多次：fail_once=首次超时可重试；fail_always=不可用；partial=素材检索部分成功")
    p.add_argument("--policy", choices=("mock", "llm"), default="mock",
                   help="决策模块：mock=离线 Mock，只验证流程（默认）；llm=真实模型（需配置 DEEPSEEK_API_KEY）")
    p.add_argument("--model", help="真实模型名，默认取 DEEPSEEK_MODEL，再默认 deepseek-v4-pro")
    p.add_argument("--max-tool-calls", type=int, help="单轮执行的工具调用上限，默认 15")
    p.add_argument("--max-model-turns", type=int, help="单轮执行的决策次数上限，默认 30")
    p.add_argument("--state-dir", default=str(STATE_DIR), help="运行状态与方案库目录（默认 .runtime/）")
    p.add_argument("--reset", action="store_true", help="先清除该请求的运行状态再执行（方案库保留，保存仍幂等）")
    p.add_argument("--json", action="store_true", help="输出完整 JSON 结果（含逐步 trace）")
    p.add_argument("--output", help="把结果（文本或 JSON）以 UTF-8 写入该文件，避免终端编码问题")
    return p


def _setup_streams() -> None:
    """控制台或 GBK 管道能显示中文就保持原编码（只把无法编码的字符替换掉）；cp1252 等不能显示中文的才改用 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            "中文≤→※".encode(stream.encoding or "ascii")
            stream.reconfigure(errors="replace")
        except (LookupError, UnicodeEncodeError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def make_policy(mode: str, model: str | None = None):
    if mode == "mock":
        return MockPolicy()
    llm = DeepSeekPolicy.from_env(model)
    if llm is None:
        raise ValueError("未配置 DEEPSEEK_API_KEY：请按 .env.example 配置，或改用 --policy mock（离线 Mock，只验证流程）")
    return llm


def _build_raw(args) -> dict:
    raw = load_json(args.input) if args.input else {}
    if not isinstance(raw, dict):
        raise ValueError("输入必须是 JSON 对象")
    overrides = {"topic": args.topic, "budget": args.budget, "deadline": args.deadline, "start_time": args.start}
    raw.update({k: v for k, v in overrides.items() if v is not None})
    if args.fault:
        simulate = raw.get("simulate") if isinstance(raw.get("simulate"), dict) else {}
        faults = dict(simulate.get("faults") or {})
        for item in args.fault:
            key, sep, kind = item.partition("=")
            if not sep:
                raise ValueError(f"--fault 格式应为 工具[:模式]=类型，收到 {item}")
            faults[key.strip()] = kind.strip()
        raw["simulate"] = {**simulate, "faults": faults}
    limits = {"max_tool_calls": args.max_tool_calls, "max_model_turns": args.max_model_turns}
    limits = {k: v for k, v in limits.items() if v is not None}
    if limits:
        raw["limits"] = {**(raw.get("limits") if isinstance(raw.get("limits"), dict) else {}), **limits}
    return raw


def main(argv=None) -> int:
    _setup_streams()
    args = build_parser().parse_args(argv)
    load_dotenv(ROOT / ".env")
    catalog = Catalog()
    try:
        req = normalize_request(_build_raw(args), catalog.fault_keys())
        policy = make_policy(args.policy, args.model)
    except (OSError, ValueError) as exc:  # json.JSONDecodeError、UnicodeDecodeError 都是 ValueError
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2
    try:
        state_dir = Path(args.state_dir)
        runs = RunStore(state_dir)
        if args.reset and req.request_key:
            runs.delete(req.request_key)
        result = PlannerAgent(catalog, runs, PlanStore(state_dir), policy).run(req)
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) if args.json else render(result)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
            print(f"结果已写入 {args.output}（状态：{result['status']}）")
        else:
            print(text)
    except Exception as exc:  # 意外错误（如状态目录不可写）：给出一行说明，不与“未完成”混用退出码
        print(f"运行异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
