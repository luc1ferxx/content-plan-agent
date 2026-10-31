"""内容任务规划 Agent：模型选择动作 → 执行工具 → 观察结果 → 模型调整下一步或结束。

“估算 → 检索 → 脚本 → 保存”是执行骨架：每一步都由决策模块（真实模型或离线 Mock）选择继续、重试、改选方案或停止。

执行器保证的硬约束（不依赖决策模块“自觉”）：
1. 每个计划版本先过“成本估算”闸门；之后每一步执行前再校验：已花费 + 剩余步骤费用 ≤ 预算、模拟时钟 + 剩余耗时 ≤ 截止时间。
2. 步骤只在依赖满足后执行；失败、部分成功如实记录，绝不改写为成功。
3. 重规划生成新版本计划，已成功（且满足新方案要求）的步骤直接复用：不重复调用、不重复计费。
4. 每次状态变化都落盘：同一请求再次提交时，已完成的直接返回；未完成的保留成功步骤、从失败处续跑。
5. 工具调用和模型决策都有次数上限，达到上限时返回明确的未完成状态。
"""
from __future__ import annotations

from datetime import datetime

from .catalog import Catalog
from .common import add_minutes, minutes_between, num
from .policy import InvalidDecision, MockPolicy, allowed_actions, normalize_decision, validate_decision
from .request import PlanRequest
from .state import PlanStore, RunStore
from .tools import ToolBox, ToolResult
from .trace import build_trace

KIND_LABEL = {"estimate": "成本估算", "search": "素材检索", "script": "脚本生成", "save": "方案保存"}
MAX_TOOL_CALLS = 15  # 单轮执行的工具调用上限（最长的合法路径是三个方案都失败重试再保存，共 12 次）
MAX_MODEL_TURNS = 30  # 单轮执行的决策次数上限
LIMIT_CODES = ("TOOL_CALL_LIMIT", "MODEL_TURN_LIMIT", "INTERNAL_ERROR")
MOCK_BOUNDARY = "离线 Mock：决策由固定规则产生，只验证流程，不证明真实模型的决策效果"
FALLBACK_BOUNDARY = "部分或全部决策回退到了离线 Mock（模型不可用或输出不合法）：这些决策只验证流程，不证明真实模型的决策效果"
COST_RULE =("费用合计 = 实际发生的工具调用费用之和：成功/部分成功按表中 fee，失败按 fee_on_failure；"
             "复用的已成功步骤不再计费。每次调用（含失败）的耗时都计入模拟时钟。")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _mode_fields(policy_name: str, decisions: list[dict] = ()) -> dict:
    """运行模式按实际发生的决策判定：配置了模型但有决策回退到 Mock 时，不能标成真实模型的结果。"""
    if policy_name.startswith("mock"):
        return {"policy": policy_name, "mode": "offline_mock", "boundary": MOCK_BOUNDARY}
    if any(d["policy"].startswith("mock") for d in decisions):
        return {"policy": policy_name, "mode": "real_model_with_mock_fallback", "boundary": FALLBACK_BOUNDARY}
    return {"policy": policy_name, "mode": "real_model", "boundary": None}


def needs_input_result(req: PlanRequest, missing: list[str], policy_name: str) -> dict:
    return {
        "run_id": None, "request_key": None, "status": "needs_input", "stop_code": "MISSING_INPUT", "repeat": False,
        **_mode_fields(policy_name), "request": req.to_dict(), "missing_inputs": missing,
        "plan": None, "plan_history": [], "steps": [], "tool_calls": [], "decisions": [], "trace": [],
        "cost": {"budget": req.budget, "spent": 0, "remaining": req.budget, "ledger": [], "rule": COST_RULE},
        "time": {"start_time": req.start_time, "clock": req.start_time, "deadline": req.deadline},
        "usage": {"tool_calls": 0, "model_turns": 0},
        "saved_plan": None, "plan_document": None, "plan_document_saved": False, "gaps": [], "resume_count": 0,
        "incomplete_reason": f"缺少必要输入：{'、'.join(missing)}。未调用任何工具、未产生费用，请补充后重新提交。",
        "message": "缺少必要输入，未开始执行",
    }


def _brief_args(kind: str, args: dict) -> dict:
    if kind == "estimate":
        return {"option_id": args["option_id"], "spent": args["spent"], "clock": args["clock"],
                "items": [f"{t}:{m}" if m else t for t, m in args["items"]]}
    if kind == "search":
        return {"topic": args["topic"], "need": args["need"]}
    if kind == "script":
        return {"topic": args["topic"], "material_ids": [m["material_id"] for m in args["materials"]]}
    return {"plan_id": args["doc"]["plan_id"], "option_id": args["doc"]["option"]["option_id"]}


class PlannerAgent:
    def __init__(self, catalog: Catalog, runs: RunStore, plans: PlanStore, policy=None, max_attempts: int = 2,
                 max_tool_calls: int = MAX_TOOL_CALLS, max_model_turns: int = MAX_MODEL_TURNS):
        self.catalog = catalog
        self.runs = runs
        self.plans = plans
        self.policy = policy or MockPolicy()
        self.fallback = MockPolicy()
        self.max_attempts = max_attempts  # 同一步骤在一轮执行中的最多尝试次数（含首次）
        self.default_limits = {"max_tool_calls": max_tool_calls, "max_model_turns": max_model_turns}
        self.tools: ToolBox | None = None
        self._policy_down: str | None = None

    # ------------------------------------------------------------------ 入口
    def run(self, req: PlanRequest) -> dict:
        missing = req.missing_inputs()
        if missing:
            return needs_input_result(req, missing, self.policy.name)
        st = self.runs.load(req.request_key)
        if st is not None and st["status"] == "completed":
            return self._result(st, repeat=True)
        self.tools = ToolBox(self.catalog, self.plans, req.faults)
        if st is None:
            st, note = self._new_state(req), ""
        else:
            note = self._begin_resume(st, req)
        st["limits"] = {**self.default_limits, **req.limits}
        if st["plan"] and not self._unsatisfied(st):  # 上次保存成功后、收尾前中断：方案已写入，直接收尾
            self._complete(st)
            return self._result(st, repeat=False)
        self._plan_initial(st, note)
        self._loop(st)
        return self._result(st, repeat=False)

    # ------------------------------------------------------------------ 状态
    def _new_state(self, req: PlanRequest) -> dict:
        key = req.request_key
        return {
            "run_id": f"R-{key[:8]}", "request_key": key, "request": req.to_dict(), "status": "running",
            "stop_code": None, "policy": self.policy.name, "created_at": _now(), "updated_at": _now(),
            "clock": req.start_time, "spent": 0, "resume_count": 0, "limits": {},
            "window_calls": 0, "window_turns": 0, "model_turns": 0,
            "plan": None, "plan_history": [], "steps": {}, "calls": [], "ledger": [], "decisions": [],
            "rejected": {}, "abandoned": {}, "unavailable": {}, "materials_found": None,
            "saved_plan": None, "document": None, "incomplete_reason": None, "gaps": [],
        }

    def _begin_resume(self, st: dict, req: PlanRequest) -> str:
        """续跑：保留全部历史与已成功步骤，只重新允许“导致上次停止的失败步骤”所用的工具。

        其他已判定不可用的工具不自动重试（避免每次重复提交都把失败链重跑一遍、重复付费）；被否决的方案仍然否决。
        需要全部重来时用 --reset。返回值写进决策记录。
        """
        st["resume_count"] += 1
        st["status"] = "running"
        st["stop_code"] = None
        st["window_calls"] = st["window_turns"] = 0
        st["incomplete_reason"] = None
        st["gaps"] = []
        st["policy"] = self.policy.name
        st["request"]["faults"] = dict(req.faults)
        st["request"]["limits"] = dict(req.limits)
        notes = []
        for s in st["steps"].values():
            if s["status"] == "running":  # 上次在调用中途被中断：补一条调用记录，结果未知按失败处理
                s["status"] = "failed"
                s["error"] = {"code": "INTERRUPTED", "retryable": True, "message": "上次运行在调用过程中中断，结果未知"}
                st["calls"].append({
                    "seq": len(st["calls"]) + 1, "step_id": s["step_id"], "tool": s["tool"], "mode": s["mode"],
                    "args": {}, "outcome": "error", "error_code": "INTERRUPTED", "retryable": True,
                    "message": "上次运行在调用过程中中断，结果未知；按失败处理，不计费、不计时",
                    "fee": 0, "minutes": 0, "clock_before": st["clock"], "clock_after": st["clock"],
                    "spent_after": st["spent"]})
                notes.append(f"{s['step_id']} 上次调用中断，按失败处理")
            elif s["status"] == "pending" and s["attempts"]:  # 已决定重试但还没发起调用就中断：最后一次真实调用是失败
                s["status"] = "failed"
                notes.append(f"{s['step_id']} 的重试未发起，保持失败")
        if st["plan"]:
            for sid in st["plan"]["steps"]:
                s = st["steps"][sid]
                key = f"{s['tool']}:{s['mode']}" if s["mode"] else s["tool"]
                if s["status"] == "failed" and st["unavailable"].pop(key, None) is not None:
                    notes.append(f"重新允许 {key}")
        self._persist(st)
        return "；".join(notes)

    def _persist(self, st: dict) -> None:
        st["updated_at"] = _now()
        self.runs.save(st)

    # ------------------------------------------------------------------ 计划
    def _need(self, st: dict, option_id: str | None = None) -> int:
        return self.catalog.need_materials(self.catalog.option(option_id or st["plan"]["option_id"]))

    def _add_step(self, st: dict, version: int, kind: str, tool: str, mode: str | None, deps: list[str]) -> dict:
        sid = f"S{len(st['steps']) + 1}"
        step = {"step_id": sid, "plan_version": version, "kind": kind, "name": KIND_LABEL[kind], "tool": tool,
                "mode": mode, "depends_on": deps, "status": "pending", "attempts": 0, "fee_charged": 0,
                "minutes_spent": 0, "result": None, "error": None, "verdict": None, "note": None}
        st["steps"][sid] = step
        return step

    def _find_reusable(self, st: dict, kind: str, need: int = 0, mode: str | None = None,
                       search_id: str | None = None) -> dict | None:
        """找可复用的已执行步骤：检索结果条数够新方案用（部分成功也算），或同模式、基于同一检索结果的脚本。"""
        for s in reversed(list(st["steps"].values())):
            if s["kind"] != kind:
                continue
            if kind == "search" and s["status"] in ("succeeded", "partial") and s["result"]["found"] >= need:
                return s
            if kind == "script" and s["status"] == "succeeded" and s["mode"] == mode and s["depends_on"][0] == search_id:
                return s
        return None

    def _projected_items(self, st: dict, opt: dict) -> list[tuple]:
        """某方案还需要执行（需付费/耗时）的步骤：估算 +（不可复用时）检索 +（不可复用时）脚本 + 保存。"""
        items = [("estimate_cost", None)]
        search = self._find_reusable(st, "search", need=self.catalog.need_materials(opt))
        if search is None:
            items.append(("search_materials", None))
        script = search and self._find_reusable(st, "script", mode=opt["script_mode"], search_id=search["step_id"])
        if not script:
            items.append(("generate_script", opt["script_mode"]))
        items.append(("save_plan", None))
        return items

    def _switch(self, st: dict, option_id: str, reason: str) -> None:
        """生成新版本计划：旧计划未执行的步骤标记为“已替换”，已成功的步骤按要求复用，并重新做成本估算。"""
        opt = self.catalog.option(option_id)
        old = st["plan"]
        version = old["version"] + 1 if old else 1
        if old:
            for sid in old["steps"]:
                self._retire(st["steps"][sid], "superseded", f"计划 v{version} 改为 {option_id}，此步骤未执行")
        need = self.catalog.need_materials(opt)
        est = self._add_step(st, version, "estimate", "estimate_cost", None, [])
        reused = []
        search = self._find_reusable(st, "search", need=need)
        if search:
            reused.append(search["step_id"])
            search["note"] = (f"部分成功（{search['result']['found']} 条），满足 {option_id} 需要的 {need} 条，复用"
                              if search["status"] == "partial" else f"已成功，{option_id} 直接复用")
        else:
            search = self._add_step(st, version, "search", "search_materials", None, [est["step_id"]])
        script = self._find_reusable(st, "script", mode=opt["script_mode"], search_id=search["step_id"])
        if script:
            reused.append(script["step_id"])
        else:
            script = self._add_step(st, version, "script", "generate_script", opt["script_mode"],
                                    [search["step_id"], est["step_id"]])
        save = self._add_step(st, version, "save", "save_plan", None, [script["step_id"], est["step_id"]])
        st["plan"] = {"version": version, "option_id": option_id,
                      "steps": [est["step_id"], search["step_id"], script["step_id"], save["step_id"]]}
        st["plan_history"].append({"version": version, "option_id": option_id, "option_name": opt["name"],
                                   "reason": reason, "reused_steps": reused})
        self._persist(st)

    @staticmethod
    def _retire(s: dict, status: str, note: str) -> None:
        """收尾计划里没走完的步骤：从未执行过的标为未执行（blocked/superseded）；执行过、重试未发起的保持失败。"""
        if s["status"] not in ("pending", "blocked"):
            return
        if s["attempts"]:
            s["status"] = "failed"
        else:
            s["status"], s["note"] = status, note

    def _plan_initial(self, st: dict, note: str = "") -> None:
        """首轮（或续跑）规划：由决策模块选定方案，“成本估算”工具把关。

        超预算的方案不在这里预筛：估算免费，让模型通过估算结果发现超限（参考实例的“估算 12 → 改选 8”）。
        但按费用表已晚于截止时间的方案不提供：估算本身要占用模拟时间，可能把唯一可行的方案也挤出截止时间。
        """
        trigger = f"续跑（第 {st['resume_count']} 次）" if st["resume_count"] else "初始规划"
        event = {"type": "plan", "text": trigger + (f"：{note}" if note else "")}
        ctx = self._context(st, event, None, exclude_current=False)
        decision = self._decide(st, ctx)
        self._record(st, event, decision, ctx)
        if decision["action"] == "plan":
            self._switch(st, decision["option_id"], decision["reason"])
        else:
            self._stop(st, ctx, decision)

    # ------------------------------------------------------------------ 执行循环
    def _satisfied(self, st: dict, s: dict) -> bool:
        if s["kind"] == "estimate":
            return s["status"] == "succeeded" and s["verdict"] == "pass"
        if s["kind"] == "search":
            return s["status"] in ("succeeded", "partial") and s["result"]["found"] >= self._need(st)
        return s["status"] == "succeeded"

    def _unsatisfied(self, st: dict) -> list[dict]:
        return [st["steps"][sid] for sid in st["plan"]["steps"] if not self._satisfied(st, st["steps"][sid])]

    def _loop(self, st: dict) -> None:
        while st["status"] == "running":
            pending = self._unsatisfied(st)
            if not pending:
                self._complete(st)
                return
            step = pending[0]
            if step["status"] != "pending" or not all(self._satisfied(st, st["steps"][d]) for d in step["depends_on"]):
                # 理论上不会出现（失败/部分成功都会先触发决策）；作为保护直接停止，绝不带着未满足的依赖继续
                self._halt(st, f"{step['step_id']} 状态为 {step['status']}，依赖未满足", "执行器检测到不一致状态，停止以免误判",
                           "INTERNAL_ERROR")
                return
            if step["kind"] != "estimate":
                event = self._guard(st, step)
                if event:
                    self._handle(st, event)
                    continue
                # 新计划的估算是必经闸门，重试已由上一次决策选定；其余每一步都由决策模块看过结果后选择
                if not step["attempts"] and not self._ask_next(st, step):
                    continue
            limit = st["limits"]["max_tool_calls"]
            if st["window_calls"] >= limit:
                self._halt(st, f"下一步是 {step['step_id']} {step['name']}",
                            f"工具调用次数达到上限 {limit}，{step['step_id']} {step['name']} 未执行", "TOOL_CALL_LIMIT")
                return
            res = self._invoke(st, step)
            event = self._observe(st, step, res)
            if event:
                self._handle(st, event)

    def _ask_next(self, st: dict, step: dict) -> bool:
        """把上一步的观察结果交给决策模块，由它选择继续下一步、改选方案或停止。返回 True 表示执行该步骤。"""
        last = st["calls"][-1]
        done = st["steps"][last["step_id"]]
        observed = done["note"] if done["kind"] == "estimate" and done["note"] else last["message"]
        event = {"type": "step_ready", "step_id": step["step_id"], "next_tool": step["tool"],
                 "text": (f"{done['step_id']} {done['name']}：{observed}；下一步是 {step['step_id']} "
                          f"{self.catalog.label(step['tool'], step['mode'])}")}
        ctx = self._context(st, event, None)
        decision = self._decide(st, ctx)
        self._record(st, event, decision, ctx)
        if decision["action"] == "call_tool":
            return True
        if decision["action"] == "switch_option":
            self._apply_switch(st, decision)
        else:
            self._stop(st, ctx, decision)
        return False

    def _args(self, st: dict, step: dict) -> dict:
        req = st["request"]
        if step["kind"] == "estimate":
            rest = [s for s in self._unsatisfied(st) if s is not step]
            items = [(step["tool"], None)] + [(s["tool"], s["mode"]) for s in rest]
            return {"option_id": st["plan"]["option_id"], "items": items, "spent": st["spent"], "clock": st["clock"]}
        if step["kind"] == "search":
            return {"topic": req["topic"], "need": self._need(st)}
        if step["kind"] == "script":
            return {"topic": req["topic"], "materials": st["steps"][step["depends_on"][0]]["result"]["materials"]}
        return {"request_key": st["request_key"], "doc": self._plan_doc(st)}

    def _invoke(self, st: dict, step: dict) -> ToolResult:
        tool, mode = step["tool"], step["mode"]
        args = self._args(st, step)
        step["status"] = "running"
        step["attempts"] += 1
        self._persist(st)  # 先落盘“执行中”，进程中断后续跑能识别
        history = [(c["tool"], c["mode"]) for c in st["calls"]]
        clock_before, price = st["clock"], self.catalog.price(tool, mode)
        res = self.tools.call(tool, mode, args, history)
        fee = price.fee if res.ok else price.fee_on_failure
        st["spent"] = num(st["spent"] + fee)
        st["clock"] = add_minutes(clock_before, price.minutes)
        st["window_calls"] += 1
        call = {"seq": len(st["calls"]) + 1, "step_id": step["step_id"], "tool": tool, "mode": mode,
                "args": _brief_args(step["kind"], args),
                "outcome": "error" if not res.ok else ("partial" if res.partial else "success"),
                "error_code": res.error_code, "retryable": None if res.ok else res.retryable, "message": res.message,
                "fee": fee, "minutes": price.minutes, "clock_before": clock_before, "clock_after": st["clock"],
                "spent_after": st["spent"]}
        st["calls"].append(call)
        if fee:
            st["ledger"].append({"call_seq": call["seq"], "step_id": step["step_id"], "tool": tool, "mode": mode,
                                 "fee": fee, "basis": "成功调用按 fee 计费" if res.ok else "失败调用按 fee_on_failure 计费"})
        step["fee_charged"] = num(step["fee_charged"] + fee)
        step["minutes_spent"] += price.minutes
        if res.ok:
            step["status"] = "partial" if res.partial else "succeeded"
            step["result"], step["error"] = res.data, None
            if step["kind"] == "search":
                st["materials_found"] = res.data["found"]
            elif step["kind"] == "save":
                st["document"] = args["doc"]
        else:
            step["status"] = "failed"
            step["error"] = {"code": res.error_code, "retryable": res.retryable, "message": res.message}
        self._persist(st)
        return res

    def _observe(self, st: dict, step: dict, res: ToolResult) -> dict | None:
        """把工具结果翻译成需要调整计划的事件；正常推进返回 None。"""
        plan, req = st["plan"], st["request"]
        if not res.ok:
            return {"type": "tool_error", "step_id": step["step_id"], "tool": step["tool"], "mode": step["mode"],
                    "code": res.error_code, "retryable": res.retryable,
                    "text": (f"{step['step_id']} {self.catalog.label(step['tool'], step['mode'])}失败："
                             f"{res.error_code}（{'可重试' if res.retryable else '不可重试'}，{res.message}）")}
        if step["kind"] == "estimate":
            d = res.data
            over = d["projected_total_fee"] - req["budget"]
            late = minutes_between(req["deadline"], d["projected_finish"])
            problems = []
            if over > 0:
                problems.append(f"预计合计 {d['projected_total_fee']} > 预算 {req['budget']}（超 {num(over)}）")
            if late > 0:
                problems.append(f"预计 {d['projected_finish']} 完成，晚于截止 {req['deadline']}（晚 {late} 分钟）")
            step["verdict"] = "rejected" if problems else "pass"
            step["note"] = "；".join(problems) if problems else (
                f"预计合计 {d['projected_total_fee']} ≤ 预算 {req['budget']}，预计 {d['projected_finish']} 完成 ≤ 截止")
            self._persist(st)
            if problems:
                return {"type": "estimate_rejected", "step_id": step["step_id"], "option_id": plan["option_id"],
                        "text": f"估算 {plan['option_id']}：" + "；".join(problems)}
            return None
        if res.partial:
            return {"type": "partial_result", "step_id": step["step_id"],
                    "text": (f"{step['step_id']} 素材检索部分成功：找到 {res.data['found']} 条 < "
                             f"{plan['option_id']} 需要的 {res.data['need']} 条")}
        return None

    def _guard(self, st: dict, step: dict) -> dict | None:
        """执行前校验（防御性）：剩余计划在当前已花费与时钟下是否仍在预算与截止时间内。"""
        req = st["request"]
        proj = self.catalog.project([(s["tool"], s["mode"]) for s in self._unsatisfied(st)], st["spent"], st["clock"])
        over = proj["projected_total_fee"] - req["budget"]
        late = minutes_between(req["deadline"], proj["projected_finish"])
        if over <= 0 and late <= 0:
            return None
        problems = []
        if over > 0:
            problems.append(f"已花费 {num(st['spent'])} + 剩余 {proj['remaining_fee']} = {proj['projected_total_fee']} > 预算 {req['budget']}")
        if late > 0:
            problems.append(f"预计 {proj['projected_finish']} 完成，晚于截止 {req['deadline']}")
        return {"type": "guard_violation", "step_id": step["step_id"], "option_id": st["plan"]["option_id"],
                "text": f"执行 {step['step_id']} 前校验未通过：" + "；".join(problems)}

    # ------------------------------------------------------------------ 决策
    def _retry_info(self, st: dict, event: dict) -> dict | None:
        if event["type"] != "tool_error" or not event["retryable"]:
            return None
        req, step = st["request"], st["steps"][event["step_id"]]
        proj = self.catalog.project([(s["tool"], s["mode"]) for s in self._unsatisfied(st)], st["spent"], st["clock"])
        fits_budget = proj["projected_total_fee"] <= req["budget"]
        fits_deadline = minutes_between(proj["projected_finish"], req["deadline"]) >= 0
        return {"step_id": step["step_id"], "attempts": step["attempts"], "max_attempts": self.max_attempts,
                "projected_total_fee": proj["projected_total_fee"], "projected_finish": proj["projected_finish"],
                "fits_budget": fits_budget, "fits_deadline": fits_deadline,
                "allowed": step["attempts"] < self.max_attempts and fits_budget and fits_deadline}

    def _mark(self, st: dict, event: dict, retry: dict | None) -> None:
        """把观察结论写进状态：被否决的方案、不可用的工具。只会越来越严，保证决策循环必然终止。"""
        cur = st["plan"]["option_id"]
        if event["type"] in ("estimate_rejected", "guard_violation", "partial_result"):
            st["rejected"][cur] = event["text"]
        elif event["type"] == "tool_error" and not (retry and retry["allowed"]):
            key = f"{event['tool']}:{event['mode']}" if event["mode"] else event["tool"]
            if not event["retryable"]:
                st["unavailable"][key] = event["code"]
            elif retry["attempts"] >= retry["max_attempts"]:
                st["unavailable"][key] = f"{event['code']}，已尝试 {retry['attempts']} 次"
            else:
                st["rejected"][cur] = f"{event['text']}；重试将超出预算或截止时间"

    def _context(self, st: dict, event: dict, retry: dict | None, exclude_current: bool = True) -> dict:
        req = st["request"]
        budget, deadline = req["budget"], req["deadline"]
        cur = st["plan"]["option_id"] if st["plan"] else None
        estimated = bool(st["plan"]) and st["steps"][st["plan"]["steps"][0]]["verdict"] == "pass"
        found = st["materials_found"]
        candidates = []
        for opt in self.catalog.options:
            oid, need, mode = opt["option_id"], self.catalog.need_materials(opt), opt["script_mode"]
            is_current = exclude_current and oid == cur
            blocks = []
            if oid in st["rejected"]:
                blocks.append(["rejected", f"已否决：{st['rejected'][oid]}"])
            if oid in st["abandoned"] and event["type"] == "step_ready":  # 正常推进时不换回来；出问题后可以再选
                blocks.append(["abandoned", f"已主动放弃：{st['abandoned'][oid]}"])
            keys = ["estimate_cost", "search_materials", "generate_script", f"generate_script:{mode}", "save_plan"]
            bad = [k for k in keys if k in st["unavailable"]]
            if bad and all(st["unavailable"][k] == "NO_MATERIAL" for k in bad):
                blocks.append(["no_material", "素材不足：素材库里没有与主题相关的素材"])
            elif bad:
                blocks.append(["tool", "依赖工具不可用：" + "、".join(f"{k}（{st['unavailable'][k]}）" for k in bad)])
            if found is not None and found < need:
                blocks.append(["materials", f"素材不足：找到 {found} 条 < 需要 {need} 条"])
            items = self._projected_items(st, opt)
            if is_current and estimated:  # 执行中的方案已通过估算，不再计入一次估算
                items = items[1:]
            proj = self.catalog.project(items, st["spent"], st["clock"])
            fee_gap = proj["projected_total_fee"] - budget
            time_gap = minutes_between(deadline, proj["projected_finish"])
            issues = []
            if fee_gap > 0:
                issues.append(f"预算缺口 {num(fee_gap)}（预计合计 {proj['projected_total_fee']} > 预算 {budget}）")
            if time_gap > 0:
                issues.append(f"时间缺口 {time_gap} 分钟（预计 {proj['projected_finish']} 完成 > 截止 {deadline}）")
            candidates.append({
                "option_id": oid, "name": opt["name"], "priority": opt["priority"], "script_mode": mode,
                "need_materials": need, "projected_total_fee": proj["projected_total_fee"],
                "projected_finish": proj["projected_finish"], "fee_gap": num(max(fee_gap, 0)),
                "time_gap_min": max(time_gap, 0), "blocks": blocks, "blocked_by": [t for _, t in blocks],
                "bad_tools": bad, "issues": issues, "is_current": is_current, "feasible": not blocks and not issues,
            })
        usable = [c for c in candidates if not c["blocks"]]
        timely = [c for c in usable if not c["time_gap_min"]]
        choices = timely or usable[:1]
        plan_view = None
        if st["plan"]:
            plan_view = {"option_id": cur, "version": st["plan"]["version"], "steps": [
                {k: st["steps"][sid][k] for k in ("step_id", "name", "tool", "mode", "status")}
                for sid in st["plan"]["steps"]]}
        ctx = {"event": event, "topic": req["topic"], "budget": budget, "spent": num(st["spent"]),
               "remaining_budget": num(budget - st["spent"]), "clock": st["clock"], "deadline": deadline,
               "minutes_left": minutes_between(st["clock"], deadline), "current_option": cur, "plan": plan_view,
               "materials_found": found, "retry": retry, "candidates": candidates, "plan_choices": choices,
               "late_options": [c for c in usable if choices and c["priority"] < choices[0]["priority"]],
               "resuming": bool(st["resume_count"]), "next_tool": event.get("next_tool")}
        ctx["allowed_actions"] = allowed_actions(ctx)
        return ctx

    def _decide(self, st: dict, ctx: dict) -> dict:
        policy, rejection = self.policy, None
        for _ in range(2):  # 不合法的决策退回给模型重选一次
            limit = st["limits"]["max_model_turns"]
            if st["window_turns"] >= limit:
                return {"action": "stop", "reason": f"本轮决策次数达到上限 {limit}", "policy": "guard",
                        "stop_code": "MODEL_TURN_LIMIT"}
            if self._policy_down:
                break
            st["window_turns"] += 1
            st["model_turns"] += 1
            try:
                decision = normalize_decision(policy.decide({**ctx, "rejection": rejection}))
                rejection = validate_decision(decision, ctx)
            except InvalidDecision as exc:  # 输出格式不对：和不合法的动作一样退回重选
                rejection = str(exc)[:160]
                continue
            except Exception as exc:  # 模型超时/网络等服务故障：熔断本轮，之后的决策直接走 Mock
                rejection = self._policy_down = self._redact(f"{type(exc).__name__}: {exc}")[:160]
                break
            if rejection is None:
                action = decision["action"]
                reason = str(decision.get("reason") or "").encode("utf-8", "replace").decode("utf-8").strip()
                return {"action": action, "policy": policy.name, "model_call": decision.get("model_call"),
                        "option_id": decision.get("option_id") if action in ("plan", "switch_option") else None,
                        "tool": decision.get("tool") if action == "call_tool" else None,
                        "reason": reason[:300] or "（未给出理由）"}
        fb = self.fallback.decide(ctx)
        fb["policy"] = f"mock（{policy.name} 的决策未采用：{self._policy_down or rejection}）"
        return fb

    def _redact(self, text: str) -> str:
        """异常信息可能带出请求头里的密钥（如密钥含换行时 urllib 报 Invalid header），写入状态/输出前先脱敏。"""
        secret = getattr(self.policy, "api_key", None)
        return text.replace(secret, "***") if secret else text

    def _record(self, st: dict, event: dict, decision: dict, ctx: dict) -> None:
        st["decisions"].append({
            "seq": len(st["decisions"]) + 1, "after_call": len(st["calls"]), "event_type": event["type"],
            "trigger": event["text"], "action": decision["action"], "option_id": decision.get("option_id"),
            "tool": decision.get("tool"), "reason": decision.get("reason"), "policy": decision.get("policy"),
            "model_call": decision.get("model_call"), "allowed_actions": ctx.get("allowed_actions"),
        })

    def _apply_switch(self, st: dict, decision: dict) -> None:
        st["abandoned"].setdefault(st["plan"]["option_id"], f"改选 {decision['option_id']}：{decision['reason']}")
        self._switch(st, decision["option_id"], decision["reason"])

    def _handle(self, st: dict, event: dict) -> None:
        retry = self._retry_info(st, event)
        self._mark(st, event, retry)
        ctx = self._context(st, event, retry)
        decision = self._decide(st, ctx)
        self._record(st, event, decision, ctx)
        if decision["action"] == "retry":
            step = st["steps"][event["step_id"]]
            step["status"] = "pending"
            step["note"] = f"第 {step['attempts']} 次尝试失败（{event['code']}），重试"
            self._persist(st)
        elif decision["action"] == "switch_option":
            self._apply_switch(st, decision)
        else:
            self._stop(st, ctx, decision)

    # ------------------------------------------------------------------ 收尾
    def _complete(self, st: dict) -> None:
        save = next(st["steps"][sid] for sid in st["plan"]["steps"] if st["steps"][sid]["kind"] == "save")
        st["status"] = "completed"
        st["saved_plan"] = save["result"]
        st["incomplete_reason"] = None
        self._persist(st)

    def _halt(self, st: dict, text: str, reason: str, code: str) -> None:
        event = {"type": "limit" if code != "INTERNAL_ERROR" else "internal", "text": text}
        ctx = self._context(st, event, None)
        decision = {"action": "stop", "reason": reason, "policy": "guard", "stop_code": code}
        self._record(st, event, decision, ctx)
        self._stop(st, ctx, decision)

    def _stop(self, st: dict, ctx: dict, decision: dict) -> None:
        if st["plan"]:
            for sid in st["plan"]["steps"]:
                self._retire(st["steps"][sid], "blocked", "运行已停止，此步骤未执行")
        full = self._context(st, ctx["event"], None, exclude_current=False)["candidates"]
        # 预算/时间只会越来越紧：静态预估就不可行的方案直接记为否决，重复提交时不再空跑估算
        for c in full:
            if c["issues"] and c["option_id"] not in st["rejected"]:
                st["rejected"][c["option_id"]] = "；".join(c["issues"])
        st["gaps"] = [{"option_id": c["option_id"], "name": c["name"],
                       "issues": c["issues"] + [t for k, t in c["blocks"] if k in ("tool", "materials", "no_material")],
                       "projected_total_fee": c["projected_total_fee"], "projected_finish": c["projected_finish"]}
                      for c in full]
        st["stop_code"] = decision.get("stop_code") or (
            "NO_MATERIAL" if "NO_MATERIAL" in st["unavailable"].values() else
            "TOOL_UNAVAILABLE" if st["unavailable"] else "NO_FEASIBLE_OPTION")
        st["incomplete_reason"] = self._stop_reason(st, ctx["event"], decision, full)
        st["status"] = "incomplete"
        self._persist(st)

    def _resumable(self, st: dict, full: list[dict]) -> bool:
        """续跑时只重新允许当前计划里失败步骤的工具；看这之后是否还有方案能在预算和截止时间内完成。"""
        retried = set()
        if st["plan"]:
            for sid in st["plan"]["steps"]:
                s = st["steps"][sid]
                if s["status"] == "failed":
                    retried.add(f"{s['tool']}:{s['mode']}" if s["mode"] else s["tool"])
        return any(not c["issues"] and set(c["bad_tools"]) <= retried
                   and not any(k in ("rejected", "materials") for k, _ in c["blocks"]) for c in full)

    def _stop_reason(self, st: dict, event: dict, decision: dict, full: list[dict]) -> str:
        req = st["request"]
        kept = [f"{s['step_id']} {s['name']}" for s in st["steps"].values()
                if s["status"] in ("succeeded", "partial") and s["kind"] != "estimate"]
        kept_text = f"已保留成功步骤 {'、'.join(kept)} 的结果（不会重复执行或计费）" if kept else ""
        no_way = "按当前已花费和模拟时钟，已没有方案能在预算和截止时间内完成，原请求重复提交不会改变结果，调整预算或截止时间将作为新请求重新执行"
        resumable = self._resumable(st, full)
        if st["stop_code"] in LIMIT_CODES:
            parts = [f"{decision['reason']}，已停止"] + ([kept_text] if kept_text else [])
            parts.append("重新提交同一请求可以从这里续跑" if resumable else no_way)
            return "；".join(parts) + "。"
        parts = [f"{event['text']}；没有同时满足预算、截止时间与工具可用性的方案，已停止"]
        if not str(decision.get("policy") or "").startswith(("mock", "guard")) and decision.get("reason"):
            parts.append(f"决策说明：{decision['reason']}")
        # 缺口摘要：在没被工具/素材卡住的方案里，找“需要改动的维度最少、差距最小”的那个，预算和时间两个维度一起说清
        open_opts = [c for c in full if not any(k in ("tool", "materials", "no_material") for k, _ in c["blocks"])
                     and (c["fee_gap"] or c["time_gap_min"])]
        if open_opts:
            c = min(open_opts, key=lambda c: (bool(c["fee_gap"]) + bool(c["time_gap_min"]), c["fee_gap"],
                                              c["time_gap_min"], c["priority"]))
            need = ([f"预算还差 {c['fee_gap']}"] if c["fee_gap"] else []) + \
                   ([f"时间还差 {c['time_gap_min']} 分钟"] if c["time_gap_min"] else [])
            enough = "预算充足" if not c["fee_gap"] else ("时间充足" if not c["time_gap_min"] else "")
            parts.append(f"最接近可行的是 {c['option_id']} {c['name']}（预计合计 {c['projected_total_fee']} / 预算 "
                         f"{req['budget']}，预计 {c['projected_finish']} 完成 / 截止 {req['deadline']}）："
                         + "、".join(need) + (f"（{enough}）" if enough else ""))
        tools_down = {k: v for k, v in st["unavailable"].items() if v != "NO_MATERIAL"}
        if tools_down:
            parts.append("不可用工具：" + "、".join(f"{k}（{v}）" for k, v in tools_down.items()))
        if st["unavailable"] and resumable:
            parts.append((f"{kept_text}；" if kept_text else "") + "工具恢复或素材补充后重新提交同一请求即可续跑，只补跑失败的步骤")
        elif kept_text:
            parts.append(f"{kept_text}；{no_way}")
        elif st["unavailable"]:
            parts.append(no_way)
        return "；".join(parts) + "。"

    def _plan_doc(self, st: dict) -> dict:
        req, plan = st["request"], st["plan"]
        opt = self.catalog.option(plan["option_id"])
        kinds = {st["steps"][sid]["kind"]: st["steps"][sid] for sid in plan["steps"]}
        save_price = self.catalog.price("save_plan")
        return {
            "plan_id": f"CP-{st['request_key'][:8]}",
            "topic": req["topic"],
            "option": {"option_id": opt["option_id"], "name": opt["name"]},
            "materials": [{k: m[k] for k in ("material_id", "title", "type")} for m in kinds["search"]["result"]["materials"]],
            "script": kinds["script"]["result"],
            "cost": {"total": num(st["spent"] + save_price.fee), "budget": req["budget"],
                     "lines": [{k: line[k] for k in ("tool", "mode", "fee", "basis")} for line in st["ledger"]]},
            "timeline": {"start_time": req["start_time"], "finish_at": add_minutes(st["clock"], save_price.minutes),
                         "deadline": req["deadline"]},
            "source": {"run_id": st["run_id"], "plan_version": plan["version"]},
        }

    def _result(self, st: dict, repeat: bool) -> dict:
        req, plan = st["request"], st["plan"]
        current = []
        if plan:
            for sid in plan["steps"]:
                s = st["steps"][sid]
                view = {k: s[k] for k in ("step_id", "name", "tool", "mode", "depends_on", "status", "attempts",
                                          "fee_charged", "note")}
                view["reused"] = s["plan_version"] != plan["version"]
                current.append(view)
        document = st.get("document")
        if document is None and plan:
            kinds = {st["steps"][sid]["kind"]: st["steps"][sid] for sid in plan["steps"]}
            if self._satisfied(st, kinds["search"]) and self._satisfied(st, kinds["script"]):
                document = self._plan_doc(st)  # 已生成但未保存的方案草稿，便于人工接手
        if repeat:
            message = "重复执行：该请求已完成，直接返回已保存的方案；本次未调用任何工具、未产生新费用。"
        elif st["status"] == "completed":
            saved = st["saved_plan"]
            message = f"方案已保存（{saved['plan_id']}，{saved['result']}），费用合计 {num(st['spent'])} / 预算 {req['budget']}。"
        else:
            message = st["incomplete_reason"]
        return {
            "run_id": st["run_id"], "request_key": st["request_key"], "status": st["status"],
            "stop_code": st["stop_code"], "repeat": repeat, **_mode_fields(st["policy"], st["decisions"]), "request": req,
            "plan": ({"version": plan["version"], "option_id": plan["option_id"],
                      "option_name": self.catalog.option(plan["option_id"])["name"], "steps": current} if plan else None),
            "plan_history": st["plan_history"],
            "steps": list(st["steps"].values()),
            "tool_calls": st["calls"],
            "decisions": st["decisions"],
            "trace": build_trace(st["calls"], st["decisions"]),
            "cost": {"budget": req["budget"], "spent": num(st["spent"]), "remaining": num(req["budget"] - st["spent"]),
                     "ledger": st["ledger"], "rule": COST_RULE},
            "time": {"start_time": req["start_time"], "clock": st["clock"], "deadline": req["deadline"],
                     "minutes_used": minutes_between(req["start_time"], st["clock"]),
                     "minutes_left": minutes_between(st["clock"], req["deadline"])},
            "usage": {"tool_calls": len(st["calls"]), "model_turns": st["model_turns"], **st["limits"]},
            "saved_plan": st["saved_plan"],
            "plan_document": document,
            "plan_document_saved": st.get("document") is not None,
            "incomplete_reason": st["incomplete_reason"],
            "gaps": st["gaps"],
            "resume_count": st["resume_count"],
            "message": message,
        }
