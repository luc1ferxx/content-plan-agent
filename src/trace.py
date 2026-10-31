"""运行记录：把决策和工具调用合并成按时间排列的轨迹，并渲染成便于评审阅读的 Markdown。

每行包含：步骤、决策与依据、工具参数、返回摘要、费用、模拟时钟、后续动作。不含模型内部思维过程。
"""
from __future__ import annotations

import json

OUTCOME = {"success": "成功", "partial": "部分成功", "error": "失败"}


def _action_text(decision: dict) -> str:
    target = decision.get("option_id") or decision.get("tool")
    return f"{decision['action']}:{target}" if target else decision["action"]


def build_trace(calls: list[dict], decisions: list[dict]) -> list[dict]:
    """一行对应一次工具调用（含调用前的决策）；最后没有引出调用的决策（如 stop）单独成行。"""
    rows = []
    for seq in range(1, len(calls) + 2):
        made = [d for d in decisions if d["after_call"] == seq - 1]
        call = calls[seq - 1] if seq <= len(calls) else None
        if call is None and not made:
            continue
        rows.append({
            "step": len(rows) + 1,
            "decisions": [{"seq": d["seq"], "trigger": d["trigger"], "action": _action_text(d), "reason": d["reason"],
                           "decided_by": d["policy"], "allowed_actions": d["allowed_actions"]} for d in made],
            "tool_call": call,
        })
    for i, row in enumerate(rows):
        following = rows[i + 1]["decisions"] if i + 1 < len(rows) else []
        if following:
            row["next_action"] = following[0]["action"]
        elif i + 1 < len(rows):
            row["next_action"] = f"call_tool:{rows[i + 1]['tool_call']['tool']}"
        else:
            row["next_action"] = "结束"
    return rows


def _cell(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ")


def _tool(call: dict) -> str:
    return f"`{call['tool']}/{call['mode']}`" if call["mode"] else f"`{call['tool']}`"


def render_trace(name: str, result: dict) -> str:
    cost, usage = result["cost"], result.get("usage") or {}
    head = [f"# 运行记录：{name}", ""]
    if result.get("boundary"):
        head += [f"> {result['boundary']}", ""]
    head += [
        f"- 决策模块：`{result['policy'] or '未调用'}`",
        f"- 结果状态：**{result['status']}**" + (f"（`{result['stop_code']}`）" if result.get("stop_code") else ""),
        f"- 费用合计：{cost['spent']} / 预算 {cost['budget']}",
        f"- 用量：{json.dumps(usage, ensure_ascii=False)}",
    ]
    if result.get("repeat"):
        head.append("- 重复执行：直接返回已保存的方案，本次没有调用工具、没有产生新费用；下面是首次执行的轨迹")
    if result.get("resume_count"):
        head.append(f"- 续跑次数：{result['resume_count']}")
    if result.get("saved_plan"):
        saved = result["saved_plan"]
        head.append(f"- 已保存方案：{saved['plan_id']}（版本 {saved['version']}，{saved['result']}）")
    if result.get("incomplete_reason"):
        head.append(f"- 未完成原因：{result['incomplete_reason']}")
    head += ["", "## 执行轨迹", ""]
    if not result["trace"]:
        return "\n".join(head + ["未调用任何工具，也没有调用模型。", ""])
    body = ["| 步骤 | 触发（观察） | 动作 · 决策者 | 决策依据 | 工具 | 参数 | 返回摘要 | 费用（累计） | 模拟时钟 | 后续动作 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for row in result["trace"]:
        ds = row["decisions"]
        trigger = "<br>".join(_cell(d["trigger"]) for d in ds) or "—"
        action = "<br>".join(f"`{_cell(d['action'])}` · {_cell(d['decided_by'])}" for d in ds) or "—"
        reason = "<br>".join(_cell(d["reason"] or "") for d in ds) or "—"
        call = row["tool_call"]
        if call:
            summary = f"{OUTCOME[call['outcome']]}：{call['message']}"
            cells = [_tool(call), f"`{_cell(call['args'])}`", _cell(summary), f"{call['fee']}（{call['spent_after']}）",
                     f"{call['clock_before'][11:]}→{call['clock_after'][11:]}"]
        else:
            cells = ["—"] * 5
        body.append("| " + " | ".join([str(row["step"]), trigger, action, reason, *cells, f"`{row['next_action']}`"]) + " |")
    return "\n".join(head + body + [""])
