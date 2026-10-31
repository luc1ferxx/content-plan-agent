"""把 Agent 结果渲染成便于人工核对的中文文本报告（--json 输出完整结构，含逐步 trace）。"""
from __future__ import annotations

STATUS = {"pending": "待执行", "running": "执行中", "succeeded": "成功", "partial": "部分成功", "failed": "失败",
          "blocked": "未执行（已停止）", "superseded": "未执行（已被替换）"}
RUN_STATUS = {"completed": "已完成", "incomplete": "未完成", "needs_input": "未开始（缺少必要输入）"}
OUTCOME = {"success": "成功", "partial": "部分成功", "error": "失败"}
ACTION = {"plan": "制定计划", "call_tool": "调用工具", "retry": "重试", "switch_option": "改选方案", "stop": "停止"}


def _tool(tool: str, mode: str | None) -> str:
    return f"{tool}/{mode}" if mode else tool


def _v(value) -> str:
    return "（缺）" if value is None else str(value)


def render(r: dict) -> str:
    req = r["request"]
    out = ["=" * 78,
           f"内容任务规划 Agent   运行 {r['run_id'] or '-'}   决策模块：{r['policy'] or '-'}"]
    if r.get("boundary"):
        out.append(r["boundary"])
    out.append(f"主题：{_v(req['topic'])} | 预算：{_v(req['budget'])} | 截止：{_v(req['deadline'])} | 开始：{req['start_time']}")
    flags = []
    if r["repeat"]:
        flags.append("重复执行，直接返回已有结果")
    if r["resume_count"]:
        flags.append(f"已续跑 {r['resume_count']} 次")
    code = f" [{r['stop_code']}]" if r.get("stop_code") else ""
    out.append(f"状态：{RUN_STATUS[r['status']]}{code}" + (f"（{'；'.join(flags)}）" if flags else ""))
    if r["status"] == "needs_input":
        out.append(f"原因：{r['incomplete_reason']}")
        return "\n".join(out)

    out += ["-" * 78, "[决策与重规划]"]
    steps_taken = sum(1 for d in r["decisions"] if d["action"] == "call_tool")
    for d in r["decisions"]:
        if d["action"] == "call_tool":
            continue
        target = f" → {d['option_id']}" if d["option_id"] else ""
        out.append(f"  #{d['seq']} 触发：{d['trigger']}")
        out.append(f"      {ACTION.get(d['action'], d['action'])}{target}  [{d['policy']}] {d['reason']}")
    if steps_taken:
        out.append(f"  另有 {steps_taken} 次“继续执行下一步”的决策，每一步的依据见 --json 输出的 trace")

    plan = r["plan"]
    current_ids = set()
    if plan:
        out.append(f"[当前任务计划 v{plan['version']}] {plan['option_id']} {plan['option_name']}")
        for s in plan["steps"]:
            current_ids.add(s["step_id"])
            deps = ",".join(s["depends_on"]) or "-"
            reused = "（复用）" if s["reused"] else ""
            note = f"  ※{s['note']}" if s["note"] else ""
            out.append(f"  {s['step_id']:<4}{s['name']}{reused}  {_tool(s['tool'], s['mode'])}  {STATUS[s['status']]}"
                       f"  尝试 {s['attempts']} 次  费用 {s['fee_charged']}  依赖 {deps}{note}")
    history = [s for s in r["steps"] if s["step_id"] not in current_ids]
    if history:
        out.append("[历史步骤（旧计划版本，保留原始状态）]")
        for s in history:
            note = f"  ※{s['note']}" if s["note"] else ""
            out.append(f"  {s['step_id']:<4}v{s['plan_version']} {s['name']}  {_tool(s['tool'], s['mode'])}  "
                       f"{STATUS[s['status']]}  尝试 {s['attempts']} 次  费用 {s['fee_charged']}{note}")

    out.append("[工具调用]")
    for c in r["tool_calls"]:
        out.append(f"  #{c['seq']:<2} {c['clock_before'][11:]}→{c['clock_after'][11:]}  {c['step_id']:<4}"
                   f"{_tool(c['tool'], c['mode'])}  {OUTCOME[c['outcome']]}  费用 {c['fee']}  累计 {c['spent_after']}"
                   f"  {c['message']}")

    cost, t = r["cost"], r["time"]
    out.append(f"[费用合计] 已花费 {cost['spent']} / 预算 {cost['budget']}（剩余 {cost['remaining']}）")
    if cost["ledger"]:
        out.append("  明细：" + " + ".join(f"#{x['call_seq']} {_tool(x['tool'], x['mode'])} {x['fee']}"
                                         for x in cost["ledger"]) + f" = {cost['spent']}")
    out.append(f"  规则：{cost['rule']}")
    out.append(f"[时间] 模拟时钟 {t['clock']}，已用 {t['minutes_used']} 分钟，距截止 {t['minutes_left']} 分钟")
    usage = r["usage"]
    out.append(f"[用量] 工具调用 {usage['tool_calls']} 次（本轮上限 {usage['max_tool_calls']}），"
               f"决策 {usage['model_turns']} 次（本轮上限 {usage['max_model_turns']}）")

    doc = r["plan_document"]
    if r["status"] == "completed":
        sp = r["saved_plan"]
        out.append(f"[结果] 方案已保存：{sp['plan_id']}（版本 {sp['version']}，{sp['result']}：{sp['message']}）")
    else:
        out.append(f"[未完成原因] {r['incomplete_reason']}")
        if any(g["issues"] for g in r["gaps"]):
            out.append("  各方案缺口（按当前模拟时钟与已花费重新预估）：")
        for g in r["gaps"]:
            if g["issues"]:
                out.append(f"  - {g['option_id']} {g['name']}：{'；'.join(g['issues'])}")
    if doc:
        script = doc["script"]
        title = script.get("title") or script.get("headline")
        label = "方案内容" if r["plan_document_saved"] else "已生成但未保存的方案草稿"
        out.append(f"[{label}] {doc['option']['option_id']} {doc['option']['name']}；素材 "
                   f"{'、'.join(m['material_id'] for m in doc['materials'])}；脚本《{title}》（{script['format']}）；"
                   f"费用 {doc['cost']['total']}，预计 {doc['timeline']['finish_at']} 完成")
    if r["repeat"]:
        out.append(r["message"])
    return "\n".join(out)
