"""验收测试：正常完成 / 缺预算或截止时间 / 工具失败 / 预算超限 / 重复执行 / 失败步骤续跑 / 运行上限。

场景用离线 Mock 走同一条循环（只验证流程）；决策模块的校验与回退用假模型验证。
运行：在项目根目录执行  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from invariants import check_result
from src import tools as tools_module
from src.agent import PlannerAgent
from src.catalog import Catalog
from src.llm import DeepSeekPolicy
from src.main import make_policy
from src.policy import MockPolicy
from src.report import render
from src.request import normalize_request
from src.state import PlanStore, RunStore

ROOT =Path(__file__).resolve().parents[1]
BASE = {"topic": "秋季新品上市", "budget": 15, "deadline": "2026-11-02 18:00", "start_time": "2026-11-02 09:00"}


class FakeLLM:
    """测试用决策模块：按事件类型返回指定决策（列表表示依次返回），其余事件按 Mock 规则走；也可以抛异常。"""

    name = "llm:fake"

    def __init__(self, replies=None, error=None):
        self.replies = {k: (list(v) if isinstance(v, list) else v) for k, v in (replies or {}).items()}
        self.error = error
        self.contexts = []

    def decide(self, ctx):
        self.contexts.append(ctx)
        if self.error:
            raise self.error
        reply = self.replies.get(ctx["event"]["type"])
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else None
        return dict(reply) if reply else MockPolicy().decide(ctx)


class AgentTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.catalog = Catalog()

    def run_agent(self, faults=None, policy=None, state_dir=None, limits=None, **fields):
        raw = {k: v for k, v in {**BASE, **fields}.items() if v is not None}
        if faults:
            raw["simulate"] = {"faults": faults}
        if limits:
            raw["limits"] = limits
        state_dir = state_dir or self.state_dir
        req = normalize_request(raw, self.catalog.fault_keys())
        agent = PlannerAgent(self.catalog, RunStore(state_dir), PlanStore(state_dir), policy or MockPolicy())
        result = agent.run(req)
        check_result(self, result)
        return result

    @staticmethod
    def calls(r, tool=None):
        return [c for c in r["tool_calls"] if tool is None or c["tool"] == tool]


class MustPassTests(AgentTestBase):
    def test_normal_completion(self):
        r = self.run_agent()
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["plan"]["option_id"], "P1")
        self.assertEqual([c["tool"] for c in r["tool_calls"]],
                         ["estimate_cost", "search_materials", "generate_script", "save_plan"])
        self.assertEqual(r["cost"]["spent"], 12)  # 2 + 10，均来自工具费用表
        self.assertEqual(r["saved_plan"]["result"], "created")
        self.assertEqual(PlanStore(self.state_dir).count(), 1)

    def test_reference_example_budget_10_switches_from_12_to_8(self):
        r = self.run_agent(budget=10)
        self.assertEqual(r["status"], "completed")
        estimates = [s for s in r["steps"] if s["kind"] == "estimate"]
        self.assertEqual([(s["result"]["option_id"], s["result"]["projected_total_fee"], s["verdict"]) for s in estimates],
                         [("P1", 12, "rejected"), ("P2", 8, "pass")])
        self.assertEqual([d["action"] for d in r["decisions"]],
                         ["plan", "switch_option", "call_tool", "call_tool", "call_tool"])
        self.assertEqual(r["plan"]["option_id"], "P2")
        self.assertEqual(r["cost"]["spent"], 8)
        self.assertNotIn("premium", [c["mode"] for c in r["tool_calls"]])  # 超预算方案的付费步骤从未执行

    def test_missing_budget_or_deadline(self):
        for missing in (("budget",), ("deadline",), ("budget", "deadline")):
            with self.subTest(missing=missing):
                r = self.run_agent(**{name: None for name in missing})
                self.assertEqual((r["status"], r["stop_code"]), ("needs_input", "MISSING_INPUT"))
                self.assertEqual(len(r["missing_inputs"]), len(missing))
                for name in missing:
                    self.assertTrue(any(name in m for m in r["missing_inputs"]))
                self.assertEqual(r["tool_calls"], [])
                self.assertEqual(r["cost"]["spent"], 0)
        self.assertFalse((self.state_dir / "runs").exists())  # 没有开始执行，也不落任何状态

    def test_tool_failure_switches_option_and_keeps_successful_steps(self):
        r = self.run_agent(faults={"generate_script:premium": "fail_always"})
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["plan"]["option_id"], "P2")
        searches = self.calls(r, "search_materials")
        self.assertEqual(len(searches), 1)  # 成功的检索被保留复用，没有重跑、没有重复计费
        self.assertIn(searches[0]["step_id"], [s["step_id"] for s in r["plan"]["steps"]])
        premium = [s for s in r["steps"] if s["mode"] == "premium"]
        self.assertEqual(premium[0]["status"], "failed")  # 失败步骤如实记为失败
        self.assertEqual(r["cost"]["spent"], 2 + 2 + 6)  # 检索 + 失败的 AI 分镜(fee_on_failure) + 标准脚本
        decision = next(d for d in r["decisions"] if d["event_type"] == "tool_error")
        self.assertEqual((decision["action"], decision["option_id"]), ("switch_option", "P2"))

    def test_timeout_is_retried_when_affordable(self):
        r = self.run_agent(faults={"generate_script:premium": "fail_once"})
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["plan"]["option_id"], "P1")
        attempts = self.calls(r, "generate_script")
        self.assertEqual([(c["outcome"], c["error_code"]) for c in attempts], [("error", "TIMEOUT"), ("success", None)])
        self.assertEqual(r["cost"]["spent"], 2 + 2 + 10)
        self.assertIn("retry", [d["action"] for d in r["decisions"]])

    def test_retry_that_would_exceed_budget_switches_instead(self):
        r = self.run_agent(budget=13, faults={"generate_script:premium": "fail_once"})
        self.assertEqual(r["status"], "completed")
        decision = next(d for d in r["decisions"] if d["event_type"] == "tool_error")
        self.assertNotIn("retry", decision["allowed_actions"])  # 重试后合计 4 + 10 = 14 > 13
        self.assertEqual(decision["option_id"], "P2")
        self.assertEqual(r["cost"]["spent"], 2 + 2 + 6)

    def test_unrecoverable_tool_failure_stops_without_fake_success(self):
        r = self.run_agent(faults={"search_materials": "fail_always"})
        self.assertEqual((r["status"], r["stop_code"]), ("incomplete", "TOOL_UNAVAILABLE"))
        self.assertEqual(r["cost"]["spent"], 0)
        self.assertEqual({s["name"]: s["status"] for s in r["plan"]["steps"]},
                         {"成本估算": "succeeded", "素材检索": "failed", "脚本生成": "blocked", "方案保存": "blocked"})
        self.assertIn("search_materials", r["incomplete_reason"])
        self.assertIsNone(r["saved_plan"])
        self.assertEqual(PlanStore(self.state_dir).count(), 0)

    def test_budget_infeasible_returns_gap_without_spending(self):
        r = self.run_agent(budget=4)
        self.assertEqual((r["status"], r["stop_code"]), ("incomplete", "NO_FEASIBLE_OPTION"))
        self.assertEqual([c["tool"] for c in r["tool_calls"]], ["estimate_cost"])
        self.assertEqual(r["cost"]["spent"], 0)
        gaps = {g["option_id"]: g["issues"] for g in r["gaps"]}
        self.assertTrue(any(i.startswith("预算缺口 1") for i in gaps["P3"]))
        self.assertIn("最接近可行的是 P3", r["incomplete_reason"])
        self.assertIn("预算还差 1（时间充足）", r["incomplete_reason"])
        self.assertNotIn("续跑", r["incomplete_reason"])  # 预算瓶颈不承诺“重新提交即可续跑”
        self.assertEqual({s["status"] for s in r["plan"]["steps"][1:]}, {"blocked"})

    def test_gap_summary_names_closest_option_on_both_axes(self):
        r = self.run_agent(budget=10, deadline="2026-11-02 09:30")  # 预算够 P2/P3，但所有方案都晚于截止
        self.assertEqual(r["status"], "incomplete")
        self.assertIn("最接近可行的是 P3", r["incomplete_reason"])
        self.assertIn("时间还差 4 分钟（预算充足）", r["incomplete_reason"])
        self.assertNotIn("预算还差", r["incomplete_reason"])

    def test_tight_deadline_picks_fastest_option(self):
        r = self.run_agent(deadline="2026-11-02 09:40")
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["plan"]["option_id"], "P3")
        self.assertEqual(r["time"]["clock"], "2026-11-02 09:33")  # 已知晚于截止的 P1/P2 不再浪费估算时间
        self.assertEqual(r["cost"]["spent"], 5)
        self.assertEqual([c["tool"] for c in r["tool_calls"]].count("estimate_cost"), 1)

    def test_deadline_boundaries_keep_feasible_options(self):
        # 截止时间恰好等于某方案的总耗时（P3=33 分钟、P2=73 分钟）时，该方案仍可行
        for deadline, option in (("2026-11-02 09:33", "P3"), ("2026-11-02 10:13", "P2"), ("2026-11-02 11:13", "P1")):
            with self.subTest(deadline=deadline):
                r = self.run_agent(deadline=deadline, state_dir=self.state_dir / deadline[-5:].replace(":", ""))
                self.assertEqual((r["status"], r["plan"]["option_id"], r["time"]["clock"]), ("completed", option, deadline))

    def test_partial_search_result_switches_to_option_it_satisfies(self):
        r = self.run_agent(topic="露营季装备推荐")
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["plan"]["option_id"], "P2")
        searches = self.calls(r, "search_materials")
        self.assertEqual([(c["outcome"], c["step_id"]) for c in searches], [("partial", "S2")])
        search_step = next(s for s in r["steps"] if s["step_id"] == "S2")
        self.assertEqual(search_step["status"], "partial")  # 部分成功不被改写成成功，但满足 P2 而被复用
        self.assertIn("S2", [s["step_id"] for s in r["plan"]["steps"]])
        self.assertEqual(r["cost"]["spent"], 8)

    def test_injected_partial_and_no_material(self):
        r = self.run_agent(faults={"search_materials": "partial"})
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P2"))
        r = self.run_agent(topic="健身打卡挑战")
        self.assertEqual((r["status"], r["stop_code"]), ("incomplete", "NO_MATERIAL"))
        self.assertEqual(self.calls(r, "search_materials")[0]["error_code"], "NO_MATERIAL")
        self.assertIn("素材库中没有", r["incomplete_reason"])
        self.assertNotIn("不可用工具", r["incomplete_reason"])  # 工具是正常的，只是没有相关素材

    def test_repeat_execution_is_idempotent(self):
        first = self.run_agent()
        second = self.run_agent()
        self.assertTrue(second["repeat"])
        self.assertEqual(second["tool_calls"], first["tool_calls"])  # 没有新的工具调用
        self.assertEqual(second["usage"]["model_turns"], first["usage"]["model_turns"])  # 也没有新的决策
        self.assertEqual(second["cost"]["spent"], 12)  # 没有重复计费
        self.assertEqual(PlanStore(self.state_dir).count(), 1)  # 没有重复保存
        third = self.run_agent(faults={"save_plan": "fail_always"})  # 已完成的请求再带故障提交也不会重跑
        self.assertTrue(third["repeat"])
        self.assertEqual(third["status"], "completed")

    def test_repeat_of_infeasible_request_does_not_rerun_tools(self):
        first = self.run_agent(budget=4)
        second = self.run_agent(budget=4)
        self.assertEqual(second["status"], "incomplete")
        self.assertEqual(len(second["tool_calls"]), len(first["tool_calls"]))
        self.assertIn("预算还差 1", second["incomplete_reason"])


class LimitTests(AgentTestBase):
    def test_tool_call_limit_returns_explicit_unfinished_state(self):
        r = self.run_agent(limits={"max_tool_calls": 3})
        self.assertEqual((r["status"], r["stop_code"]), ("incomplete", "TOOL_CALL_LIMIT"))
        self.assertEqual(len(r["tool_calls"]), 3)
        self.assertIn("上限 3", r["incomplete_reason"])
        self.assertIn("续跑", r["incomplete_reason"])
        self.assertEqual(r["plan"]["steps"][-1]["status"], "blocked")
        wanted, stopped = r["decisions"][-2:]  # 超限的调用留痕：决策选了它，执行器拒绝
        self.assertEqual((wanted["action"], wanted["tool"]), ("call_tool", "save_plan"))
        self.assertEqual((stopped["action"], stopped["policy"]), ("stop", "guard"))
        self.assertIsNone(r["saved_plan"])
        resumed = self.run_agent()  # 上限只限本轮：重新提交从这里续跑
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual([c["tool"] for c in resumed["tool_calls"][3:]], ["estimate_cost", "save_plan"])

    def test_model_turn_limit_returns_explicit_unfinished_state(self):
        r = self.run_agent(limits={"max_model_turns": 2})
        self.assertEqual((r["status"], r["stop_code"]), ("incomplete", "MODEL_TURN_LIMIT"))
        self.assertEqual(r["usage"]["model_turns"], 2)
        self.assertIn("决策次数达到上限 2", r["incomplete_reason"])

    def test_resume_is_promised_only_when_it_can_still_succeed(self):
        r = self.run_agent(deadline="2026-11-02 09:40", faults={"generate_script:lite": "fail_always"})
        self.assertEqual(r["stop_code"], "TOOL_UNAVAILABLE")
        self.assertNotIn("即可续跑", r["incomplete_reason"])  # 时间已经不够，工具恢复也来不及
        self.assertIn("已没有方案能在预算和截止时间内完成", r["incomplete_reason"])
        r = self.run_agent(deadline="2026-11-02 11:13", limits={"max_tool_calls": 3}, state_dir=self.state_dir / "limit")
        self.assertEqual(r["stop_code"], "TOOL_CALL_LIMIT")
        self.assertNotIn("可以从这里续跑", r["incomplete_reason"])  # 续跑要先重新估算 1 分钟，保存就赶不上截止了

    def test_default_limits_cover_the_longest_legal_path(self):
        faults = {"generate_script": "fail_once", "save_plan": "fail_once"}
        r = self.run_agent(faults=faults, budget=20)
        self.assertEqual(r["status"], "completed")
        self.assertLessEqual(r["usage"]["tool_calls"], r["usage"]["max_tool_calls"])


class ResumeTests(AgentTestBase):
    def test_resume_only_reruns_failed_save(self):
        r1 = self.run_agent(faults={"save_plan": "fail_always"})
        self.assertEqual(r1["status"], "incomplete")
        self.assertEqual(r1["cost"]["spent"], 12)
        self.assertIsNone(PlanStore(self.state_dir).get(r1["request_key"]))  # 失败 = 确实没写入
        self.assertFalse(r1["plan_document_saved"])
        self.assertIsNotNone(r1["plan_document"])  # 已生成的草稿保留，便于人工接手
        r2 = self.run_agent()
        self.assertEqual(r2["status"], "completed")
        self.assertEqual(r2["resume_count"], 1)
        new_calls = r2["tool_calls"][len(r1["tool_calls"]):]
        self.assertEqual([c["tool"] for c in new_calls], ["estimate_cost", "save_plan"])
        self.assertEqual(r2["cost"]["spent"], 12)
        self.assertEqual(len(self.calls(r2, "search_materials")), 1)
        self.assertEqual(len(self.calls(r2, "generate_script")), 1)
        failed_saves = [s for s in r2["steps"] if s["kind"] == "save" and s["status"] == "failed"]
        self.assertEqual(len(failed_saves), 1)  # 上一轮失败的保存仍记为失败

    def test_resume_after_every_script_mode_failed(self):
        r1 = self.run_agent(faults={"generate_script": "fail_always"})
        self.assertEqual(r1["status"], "incomplete")
        self.assertEqual([h["option_id"] for h in r1["plan_history"]], ["P1", "P2", "P3"])
        self.assertEqual(r1["cost"]["spent"], 2 + 2 + 1 + 0)
        self.assertIn("续跑", r1["incomplete_reason"])  # 工具类失败：承诺可续跑
        # 故障未恢复时重复提交：只重试导致停止的那一步（图文脚本），不把整条失败链重跑一遍
        r2 = self.run_agent(faults={"generate_script": "fail_always"})
        new_calls = r2["tool_calls"][len(r1["tool_calls"]):]
        self.assertEqual([(c["tool"], c["mode"]) for c in new_calls],
                         [("estimate_cost", None), ("generate_script", "lite")])
        self.assertEqual(r2["cost"]["spent"], r1["cost"]["spent"])  # 图文脚本失败不收费，花费不增长
        # 工具恢复后续跑：继续当前方案 P3，检索结果复用，此前判定不可用的模式不自动重试
        r3 = self.run_agent()
        self.assertEqual((r3["status"], r3["plan"]["option_id"]), ("completed", "P3"))
        self.assertEqual(r3["cost"]["spent"], 5 + 3)
        self.assertEqual(len(self.calls(r3, "search_materials")), 1)

    def test_fail_once_counts_attempts_across_resume(self):
        r1 = self.run_agent(faults={"save_plan": "fail_always"})
        self.assertEqual(r1["status"], "incomplete")
        # “首次调用超时”按持久化的调用历史判断：上一轮已调用过 save_plan，续跑时不会再注入超时
        r2 = self.run_agent(faults={"save_plan": "fail_once"})
        self.assertEqual(r2["status"], "completed")
        resumed = r2["tool_calls"][len(r1["tool_calls"]):]
        self.assertEqual([(c["tool"], c["outcome"]) for c in resumed],
                         [("estimate_cost", "success"), ("save_plan", "success")])

    def test_crash_during_tool_call_is_recorded_and_resumed(self):
        original = tools_module.ToolBox.call

        def crash_on_script(toolbox, tool, mode, args, history):
            if tool == "generate_script":
                raise KeyboardInterrupt  # 模拟进程在调用过程中被杀掉
            return original(toolbox, tool, mode, args, history)

        with mock.patch.object(tools_module.ToolBox, "call", crash_on_script), self.assertRaises(KeyboardInterrupt):
            self.run_agent()
        r = self.run_agent()  # check_result 会核对“尝试次数 = 调用记录数”
        self.assertEqual(r["status"], "completed")
        interrupted = [c for c in r["tool_calls"] if c["error_code"] == "INTERRUPTED"]
        self.assertEqual([(c["tool"], c["fee"], c["minutes"]) for c in interrupted], [("generate_script", 0, 0)])
        self.assertEqual(len(self.calls(r, "search_materials")), 1)
        resume = next(d for d in r["decisions"] if d["trigger"].startswith("续跑"))
        self.assertIn("S3 上次调用中断", resume["trigger"])  # 续跑的决策记录写明了中断

    def test_crash_after_save_before_finishing_just_finishes(self):
        original = PlannerAgent._complete
        calls = []

        def crash_once(agent, st):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt  # 保存已落盘，运行状态还没改成完成
            return original(agent, st)

        with mock.patch.object(PlannerAgent, "_complete", crash_once):
            with self.assertRaises(KeyboardInterrupt):
                self.run_agent()
            r = self.run_agent()
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(self.calls(r, "save_plan")), 1)  # 不重复保存
        self.assertEqual(PlanStore(self.state_dir).get(r["request_key"])["version"], 1)

    def test_crash_right_after_partial_search_does_not_buy_it_again(self):
        original = PlannerAgent._observe

        def crash_on_partial(agent, st, step, res):
            if res.partial:
                raise KeyboardInterrupt  # 检索结果已落盘，还没来得及决策
            return original(agent, st, step, res)

        with mock.patch.object(PlannerAgent, "_observe", crash_on_partial), self.assertRaises(KeyboardInterrupt):
            self.run_agent(topic="露营季装备推荐")
        r = self.run_agent(topic="露营季装备推荐")
        self.assertEqual((r["status"], r["plan"]["option_id"], r["cost"]["spent"]), ("completed", "P2", 8))
        self.assertEqual(len(self.calls(r, "search_materials")), 1)

    def test_crash_after_retry_decision_keeps_step_failed(self):
        original = PlannerAgent._guard

        def crash_before_retry(agent, st, step):
            if step["attempts"]:
                raise KeyboardInterrupt  # 已决定重试、尚未发起调用时中断
            return original(agent, st, step)

        with mock.patch.object(PlannerAgent, "_guard", crash_before_retry), self.assertRaises(KeyboardInterrupt):
            self.run_agent(faults={"generate_script:premium": "fail_once"})
        r = self.run_agent()
        self.assertEqual(r["status"], "completed")
        first_premium = next(s for s in r["steps"] if s["mode"] == "premium")
        self.assertEqual((first_premium["status"], first_premium["attempts"]), ("failed", 1))  # 不会被标成“未执行”


class DecisionLoopTests(AgentTestBase):
    def test_model_chooses_the_action_at_every_step(self):
        llm = FakeLLM()
        r = self.run_agent(policy=llm)
        self.assertEqual([c["event"]["type"] for c in llm.contexts], ["plan", "step_ready", "step_ready", "step_ready"])
        self.assertEqual([d["tool"] for d in r["decisions"][1:]], ["search_materials", "generate_script", "save_plan"])
        self.assertTrue(all(d["policy"] == "llm:fake" for d in r["decisions"]))
        self.assertEqual(r["mode"], "real_model")
        self.assertIsNone(r["boundary"])

    def test_model_sees_tool_results_and_can_replan_when_things_look_fine(self):
        llm = FakeLLM({"step_ready": [{"action": "switch_option", "option_id": "P2", "reason": "预算余量小，改选更稳妥的方案"}]})
        r = self.run_agent(policy=llm, budget=12)
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P2"))
        self.assertEqual(r["cost"]["spent"], 8)
        self.assertIn("S1 成本估算：预计合计 12 ≤ 预算 12", llm.contexts[1]["event"]["text"])
        self.assertEqual(llm.contexts[1]["allowed_actions"],
                         ["call_tool:search_materials", "switch_option:P2", "switch_option:P3"])

    def test_cost_is_learned_from_the_estimate_tool_not_shown_at_planning(self):
        captured = []

        def transport(url, body):
            state = json.loads(body["messages"][1]["content"])
            captured.append(state)
            allowed = state["allowed_actions"][0]
            kind, _, target = allowed.partition(":")
            key = "tool" if kind == "call_tool" else "option_id"
            return {"choices": [{"message": {"content": json.dumps({"action": kind, key: target, "reason": "测试"})}}]}

        r = self.run_agent(budget=10, policy=DeepSeekPolicy("test-key", transport=transport))
        self.assertEqual(r["plan"]["option_id"], "P2")
        self.assertNotIn("projected_total_fee", captured[0]["candidates"][0])  # 首次规划看不到成本预估
        self.assertIn("预计合计 12", captured[1]["last_observation"])  # 估算结果作为观察交给模型

    def test_model_is_told_the_truth_about_the_current_option(self):
        captured = []

        def transport(url, body):
            state = json.loads(body["messages"][1]["content"])
            captured.append(state)
            kind, _, target = state["allowed_actions"][0].partition(":")
            key = "tool" if kind == "call_tool" else "option_id"
            return {"choices": [{"message": {"content": json.dumps({"action": kind, key: target, "reason": "测试"})}}]}

        self.run_agent(policy=DeepSeekPolicy("test-key", transport=transport))
        ready = next(s for s in captured if s["event"] == "step_ready")
        current = next(c for c in ready["candidates"] if c["is_current"])
        # 当前方案可行时必须如实告诉模型，否则模型会误判并主动降级
        self.assertEqual((current["option_id"], current["feasible"], current["blocked_by"]), ("P1", True, []))
        self.assertEqual((current["projected_total_fee"], current["projected_finish"]), (12, "2026-11-02 11:13"))
        self.assertNotIn("switch_option:P1", ready["allowed_actions"])

    def test_voluntarily_abandoned_option_cannot_be_chosen_again(self):
        llm = FakeLLM({"step_ready": [{"action": "switch_option", "option_id": "P2", "reason": "先换"},
                                      {"action": "switch_option", "option_id": "P1", "reason": "换回去"}]})
        r = self.run_agent(policy=llm)
        self.assertEqual(r["plan"]["option_id"], "P2")  # 第二次改选不合法，被退回后由 Mock 继续
        self.assertEqual([h["option_id"] for h in r["plan_history"]], ["P1", "P2"])

    def test_invalid_decision_is_returned_to_the_model_once(self):
        llm = FakeLLM({"estimate_rejected": [{"action": "switch_option", "option_id": "P1", "reason": "坚持旗舰"},
                                             {"action": "switch_option", "option_id": "P3", "reason": "改选图文"}]})
        r = self.run_agent(budget=10, policy=llm)
        self.assertEqual(r["plan"]["option_id"], "P3")
        decision = next(d for d in r["decisions"] if d["event_type"] == "estimate_rejected")
        self.assertEqual(decision["policy"], "llm:fake")
        retried = [c for c in llm.contexts if c.get("rejection")]
        self.assertEqual(len(retried), 1)
        self.assertIn("switch_option:P1", retried[0]["rejection"])

    def test_repeatedly_invalid_decision_falls_back_to_mock(self):
        bad = {"action": "switch_option", "option_id": "P1", "reason": "坚持旗舰"}
        r = self.run_agent(budget=10, policy=FakeLLM({"estimate_rejected": [bad, bad]}))
        self.assertEqual(r["plan"]["option_id"], "P2")
        decision = next(d for d in r["decisions"] if d["event_type"] == "estimate_rejected")
        self.assertTrue(decision["policy"].startswith("mock（llm:fake 的决策未采用"))

    def test_llm_cannot_stop_while_alternative_exists(self):
        bad = {"action": "stop", "reason": "放弃"}
        r = self.run_agent(budget=10, policy=FakeLLM({"estimate_rejected": [bad, bad]}))
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P2"))

    def test_llm_exception_falls_back_to_mock_for_the_rest_of_the_run(self):
        llm = FakeLLM(error=TimeoutError("network down"))
        r = self.run_agent(budget=10, policy=llm)
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P2"))
        self.assertIn("TimeoutError", r["decisions"][0]["policy"])
        self.assertEqual(len(llm.contexts), 1)  # 熔断：之后不再调用模型
        self.assertEqual(r["mode"], "real_model_with_mock_fallback")  # 不能把 Mock 的决策标成真实模型的结果
        self.assertIn("回退到了离线 Mock", r["boundary"])
        self.assertIn("回退到了离线 Mock", render(r))

    def test_action_written_like_an_allowed_token_is_understood(self):
        # 模型把 allowed_actions 的写法原样填进 action 时，不能被当成 stop 执行
        llm = FakeLLM({"plan": {"action": "plan:P1", "reason": "效果最好"},
                       "estimate_rejected": {"action": "switch_option:P2", "reason": "预算内"},
                       "step_ready": {"action": "call_tool:search_materials", "reason": "继续"}})
        r = self.run_agent(budget=10, policy=llm, limits={"max_tool_calls": 3})
        self.assertEqual((r["plan"]["option_id"], [d["action"] for d in r["decisions"]][:3]),
                         ("P2", ["plan", "switch_option", "call_tool"]))
        self.assertTrue(all(d["policy"] == "llm:fake" for d in r["decisions"][:3]))

    def test_unknown_action_is_never_executed_as_a_stop(self):
        r = self.run_agent(policy=FakeLLM({"plan": {"action": "launch", "reason": "直接发布"}}))
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P1"))
        self.assertLess(len(r["decisions"][0]["policy"]), 300)

    def test_voluntarily_abandoned_option_is_available_again_after_a_failure(self):
        llm = FakeLLM({"step_ready": [{"action": "switch_option", "option_id": "P2", "reason": "先换"}]})
        r = self.run_agent(policy=llm, faults={"generate_script:standard": "fail_always"})
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P1"))  # 可行的方案不能因主动放弃而永远不可用
        self.assertEqual([h["option_id"] for h in r["plan_history"]], ["P1", "P2", "P1"])

    def test_api_key_never_leaks_into_output_or_state(self):
        secret = "sk-test-SECRET-123"

        def transport(url, body):  # urllib 遇到含换行的密钥时，异常信息会带出整个请求头
            raise ValueError(f"Invalid header value b'Bearer {secret}\\r'")

        r = self.run_agent(budget=10, policy=DeepSeekPolicy(secret, transport=transport))
        self.assertEqual((r["status"], r["plan"]["option_id"]), ("completed", "P2"))
        self.assertNotIn(secret, json.dumps(r, ensure_ascii=False))
        for path in self.state_dir.rglob("*.json"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-abc\r\n"}):
            self.assertEqual(DeepSeekPolicy.from_env().api_key, "sk-abc")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-a b"}):
            self.assertIsNone(DeepSeekPolicy.from_env())

    def test_model_output_with_extra_text_is_parsed(self):
        replies = iter(['{"action": "plan", "option_id": "P1", "reason": "效果最好"}\n{"action": "plan", "option_id": "P2"}',
                        '说明如下：\n```json\n{"action": "call_tool", "tool": "search_materials", "reason": "继续"}\n```\n以上。'])

        def transport(url, body):
            return {"choices": [{"message": {"content": next(replies)}}]}

        policy = DeepSeekPolicy("test-key", transport=transport)
        ctx = self._plan_context()
        self.assertEqual(policy.decide(ctx)["option_id"], "P1")  # 取第一个 JSON 对象
        self.assertEqual(policy.decide(ctx)["tool"], "search_materials")

    def test_unparsable_model_output_is_returned_to_the_model_not_treated_as_an_outage(self):
        garbage = iter(["不是 JSON"])

        def transport(url, body):
            content = next(garbage, None)
            if content is None:
                kind, _, target = json.loads(body["messages"][1]["content"])["allowed_actions"][0].partition(":")
                key = "tool" if kind == "call_tool" else "option_id"
                content = json.dumps({"action": kind, key: target, "reason": "重选"})
            return {"choices": [{"message": {"content": content}}]}

        r = self.run_agent(policy=DeepSeekPolicy("test-key", transport=transport))
        self.assertEqual(r["status"], "completed")
        self.assertTrue(all(d["policy"] == "llm:deepseek-v4-pro" for d in r["decisions"]))  # 重选后仍由模型决策，没有熔断

    def test_deepseek_policy_request_and_parsing(self):
        captured = {}

        def transport(url, body):
            captured.update(url=url, body=body)
            content = '```json\n{"action": "plan", "option_id": "P1", "reason": "效果最好"}\n```'
            return {"choices": [{"message": {"content": content}}]}

        decision = DeepSeekPolicy("test-key", transport=transport).decide(self._plan_context())
        self.assertEqual((decision["action"], decision["option_id"]), ("plan", "P1"))
        self.assertEqual(set(decision["model_call"]), {"response_id", "model", "total_tokens"})
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertIn("plan:P1", json.loads(captured["body"]["messages"][1]["content"])["allowed_actions"])

    def _plan_context(self):
        captured = []

        class Capture(MockPolicy):
            def decide(inner, ctx):
                captured.append(ctx)
                return super().decide(ctx)

        self.run_agent(policy=Capture(), state_dir=self.state_dir / "ctx")
        return captured[0]

    def test_llm_without_key_is_a_configuration_error(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), self.assertRaises(ValueError):
            make_policy("llm")


class InputAndCliTests(AgentTestBase):
    def test_format_errors_raise(self):
        bad_inputs = [{"budget": "十"}, {"budget": -1}, {"budget": True}, {"deadline": "11/02 18:00"},
                      {"simulate": {"faults": {"unknown_tool": "fail_once"}}},
                      {"simulate": {"faults": {"save_plan": "partial"}}},
                      {"request_id": float("nan")}, {"topic": "\ud800"},
                      {"limits": {"max_tool_calls": 0}}, {"limits": {"max_steps": 3}}, {"limits": 3}]
        for bad in bad_inputs:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_request({**BASE, **bad}, self.catalog.fault_keys())
        self.assertEqual(normalize_request({**BASE, "budget": 10 ** 30}).budget, 10 ** 30)  # 大整数不再崩溃
        self.assertEqual(normalize_request({**BASE, "budget": 10.1}).budget, 10.1)

    def test_decimal_budget_has_no_float_noise(self):
        r = self.run_agent(budget=10.1)
        self.assertEqual((r["cost"]["spent"], r["cost"]["remaining"]), (8, 2.1))

    def test_request_key_ignores_start_time_faults_and_limits(self):
        a = normalize_request(BASE)
        b = normalize_request({**BASE, "start_time": "2026-11-01 08:00", "limits": {"max_tool_calls": 3},
                               "simulate": {"faults": {"save_plan": "fail_once"}}})
        c = normalize_request({**BASE, "budget": 15.0})
        self.assertEqual(a.request_key, b.request_key)
        self.assertEqual(a.request_key, c.request_key)
        self.assertNotEqual(a.request_key, normalize_request({**BASE, "budget": 16}).request_key)

    def cli(self, *args):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, "-m", "src.main", "--state-dir", str(self.state_dir), *args],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)

    def test_cli_accepts_bom_and_utf16_input_and_output_file(self):
        text = json.dumps({**BASE, "budget": 11}, ensure_ascii=False)
        for name, data in (("bom.json", text.encode("utf-8-sig")), ("u16.json", text.encode("utf-16"))):
            with self.subTest(name=name):
                path = self.state_dir / name
                path.write_bytes(data)
                out = self.state_dir / f"{name}.out.json"
                proc = self.cli("--input", str(path), "--json", "--output", str(out))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["status"], "completed")

    def test_cli_exit_codes_and_outputs(self):
        ok = self.cli("--input", "examples/inputs/normal.json")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("方案已保存", ok.stdout)
        self.assertIn("离线 Mock", ok.stdout)  # 默认入口标明边界
        self.assertEqual(self.cli("--input", "examples/inputs/missing_deadline.json").returncode, 1)
        self.assertEqual(self.cli("--input", "examples/inputs/infeasible_budget.json").returncode, 1)
        self.assertEqual(self.cli("--input", "examples/inputs/normal.json", "--fault", "nope=fail_once").returncode, 2)
        self.assertEqual(self.cli("--input", "examples/inputs/normal.json", "--budget", "abc").returncode, 2)
        data = json.loads(self.cli("--input", "examples/inputs/over_budget.json", "--json").stdout)
        self.assertEqual((data["status"], data["plan"]["option_id"], data["cost"]["spent"]), ("completed", "P2", 8))
        self.assertEqual((data["mode"], data["policy"]), ("offline_mock", "mock"))
        self.assertTrue(data["trace"])

    def test_cli_limit_and_resume(self):
        first = self.cli("--input", "examples/inputs/resume_ok.json", "--fault", "save_plan=fail_always")
        self.assertEqual(first.returncode, 1)
        second = self.cli("--input", "examples/inputs/resume_ok.json", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["resume_count"], 1)
        limited = self.cli("--input", "examples/inputs/normal.json", "--max-tool-calls", "2", "--json")
        self.assertEqual(limited.returncode, 1)
        self.assertEqual(json.loads(limited.stdout)["stop_code"], "TOOL_CALL_LIMIT")


class InvariantGridTests(AgentTestBase):
    """在一组预算 × 故障组合上跑一遍，确认任何路径都不超预算、不超截止时间、不把失败记成成功。"""

    def test_invariants_hold_across_budgets_and_faults(self):
        fault_sets = [None, {"generate_script:premium": "fail_once"}, {"generate_script:premium": "fail_always"},
                      {"generate_script:standard": "fail_always"}, {"search_materials": "partial"},
                      {"save_plan": "fail_once"}, {"generate_script": "fail_once"}]
        for budget in range(0, 21):
            for i, faults in enumerate(fault_sets):
                with self.subTest(budget=budget, faults=faults):
                    r = self.run_agent(budget=budget, faults=faults, state_dir=self.state_dir / f"{budget}-{i}")
                    self.assertIn(r["status"], ("completed", "incomplete"))
                    if budget >= 12 and not faults:
                        self.assertEqual(r["plan"]["option_id"], "P1")
                    if budget < 5:
                        self.assertEqual(r["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
