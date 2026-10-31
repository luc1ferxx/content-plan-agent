"""决策接口与离线 Mock。

执行器在每一步都调用 decide(ctx)，由决策模块选择下一步动作（模型选择动作 → 执行工具 → 观察结果 → 调整或结束）：
- plan：选定方案，生成任务计划（开始或续跑时）
- call_tool：调用计划里的下一个工具
- retry：重试刚失败的步骤
- switch_option：改选替代方案，重新估算
- stop：停止并说明缺口

ctx["allowed_actions"] 由执行器按步骤依赖、预算、截止时间和工具状态算出。决策无论来自谁都要过 validate_decision：
不合法的决策退回给模型重选一次，仍不合法或调用失败时改用 MockPolicy，并在决策记录里写明。
"""
from __future__ import annotations


ACTIONS = ("plan", "call_tool", "retry", "switch_option", "stop")


class InvalidDecision(ValueError):
    """模型输出无法解析成决策。和不合法的动作一样退回给模型重选，不算模型服务故障。"""


def allowed_actions(ctx: dict) -> list[str]:
    kind = ctx["event"]["type"]
    if kind == "plan":
        return [f"plan:{c['option_id']}" for c in ctx["plan_choices"]] or ["stop"]
    actions = [f"call_tool:{ctx['next_tool']}"] if kind == "step_ready" else []
    retry = ctx.get("retry")
    if retry and retry["allowed"]:
        actions.append("retry")
    actions += [f"switch_option:{c['option_id']}" for c in ctx["candidates"] if c["feasible"] and not c["is_current"]]
    return actions or ["stop"]


def normalize_decision(decision):
    """模型有时把 allowed_actions 里的写法原样填进 action（如 "plan:P1"），这里拆成标准字段。"""
    action = decision.get("action") if isinstance(decision, dict) else None
    if isinstance(action, str) and ":" in action:
        kind, _, target = action.partition(":")
        return {**decision, "action": kind, ("tool" if kind == "call_tool" else "option_id"): target}
    return decision


def action_token(decision: dict) -> str:
    action = decision.get("action")
    if action in ("plan", "switch_option"):
        return f"{action}:{decision.get('option_id')}"
    if action == "call_tool":
        return f"call_tool:{decision.get('tool')}"
    return str(action)


def validate_decision(decision, ctx: dict) -> str | None:
    if not isinstance(decision, dict):
        return "决策不是 JSON 对象"
    token = action_token(decision)
    if decision.get("action") not in ACTIONS or token not in ctx["allowed_actions"]:
        return f"动作 {token[:80]} 不在允许列表 {ctx['allowed_actions']} 中"
    return None


class MockPolicy:
    """离线 Mock：用固定规则代替模型，走同一条循环。只验证流程，不证明真实模型的决策效果。

    规则：按计划继续；能重试就重试（保住原方案效果）；否则改选 priority 最小的可行方案；都不行才停止。
    """

    name = "mock"

    def decide(self, ctx: dict) -> dict:
        kind, _, target = ctx["allowed_actions"][0].partition(":")
        if kind == "plan":
            return {"action": "plan", "option_id": target, "reason": self._plan_reason(ctx, target)}
        if kind == "call_tool":
            return {"action": "call_tool", "tool": target, "reason": "上一步结果符合预期，按计划执行下一步"}
        if kind == "retry":
            retry = ctx["retry"]
            return {"action": "retry", "reason": (
                f"临时错误，已尝试 {retry['attempts']}/{retry['max_attempts']} 次；重试后预计合计 "
                f"{retry['projected_total_fee']} ≤ 预算 {ctx['budget']}，预计 {retry['projected_finish']} 完成 ≤ 截止 "
                f"{ctx['deadline']}，先重试以保住原方案")}
        if kind == "switch_option":
            best = self._candidate(ctx, target)
            return {"action": "switch_option", "option_id": target, "reason": (
                f"改选 {target} {best['name']}：按费用表预计合计 {best['projected_total_fee']} ≤ 预算 "
                f"{ctx['budget']}，预计 {best['projected_finish']} 完成 ≤ 截止 {ctx['deadline']}，"
                f"是可行候选中效果最好的；已成功步骤直接复用")}
        return {"action": "stop", "reason": "没有可重试的步骤，也没有同时满足预算、截止时间与工具可用性的替代方案"}

    @staticmethod
    def _candidate(ctx: dict, option_id: str) -> dict:
        return next(c for c in ctx["candidates"] if c["option_id"] == option_id)

    def _plan_reason(self, ctx: dict, option_id: str) -> str:
        chosen = self._candidate(ctx, option_id)
        if ctx["resuming"]:
            reason = f"保留已成功步骤，按 {option_id} {chosen['name']} 重建计划，先重新估算剩余成本"
        else:
            reason = f"选择效果优先的 {option_id} {chosen['name']}，先调用成本估算确认预算与截止时间"
        if ctx["late_options"]:
            reason += "；跳过 " + "、".join(f"{c['option_id']}（按费用表预计 {c['projected_finish']} 完成，已晚于截止）"
                                          for c in ctx["late_options"])
        return reason
