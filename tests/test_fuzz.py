"""随机决策测试：不管决策模块怎么选，执行器的硬约束都要成立（固定随机种子，可复现）。

两种决策模块：在允许动作里随机选；时不时返回垃圾输出或抛异常。
每个请求都带随机故障和低上限，并换故障重复提交，覆盖续跑和重复执行。
"""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from invariants import check_result
from src.agent import PlannerAgent
from src.catalog import Catalog
from src.policy import InvalidDecision
from src.request import normalize_request
from src.state import PlanStore, RunStore

TOPICS = ("秋季新品上市", "露营季装备推荐", "咖啡节", "健身打卡挑战")
DEADLINES = ("2026-11-02 09:30", "2026-11-02 09:33", "2026-11-02 09:40", "2026-11-02 10:13", "2026-11-02 11:13", "2026-11-02 18:00")
FAULTS = ("fail_once", "fail_always")
FAULT_KEYS = ("estimate_cost", "search_materials", "generate_script", "generate_script:premium",
              "generate_script:standard", "generate_script:lite", "save_plan")


class RandomPolicy:
    name = "llm:random"

    def __init__(self, rng: random.Random):
        self.rng = rng

    def decide(self, ctx):
        kind, _, target = self.rng.choice(ctx["allowed_actions"]).partition(":")
        key = "tool" if kind == "call_tool" else "option_id"
        return {"action": kind, key: target, "reason": "随机选择"}


class GarbagePolicy(RandomPolicy):
    name = "llm:garbage"

    def decide(self, ctx):
        roll = self.rng.random()
        if roll < 0.15:
            return self.rng.choice([None, 3, "stop", [], {}, {"action": None}, {"action": ["plan"]}, {"action": "launch"},
                                    {"action": "plan"}, {"action": "call_tool", "tool": "delete_everything"},
                                    {"action": "switch_option", "option_id": "P9"}, {"action": "stop", "reason": "放弃"},
                                    {"action": "plan:P9"}, {"action": "x" * 5000}])
        if roll < 0.2:
            raise InvalidDecision("模型输出不是合法 JSON")
        if roll < 0.25:
            return dict(super().decide(ctx), reason="\ud83d 含非法字符的理由")
        if roll < 0.3:
            return {"action": self.rng.choice(ctx["allowed_actions"]), "reason": "把 allowed_actions 的写法填进 action"}
        return super().decide(ctx)


class FuzzTests(unittest.TestCase):
    def run_fuzz(self, policy_class, seed: int, cases: int):
        catalog = Catalog()
        rng = random.Random(seed)
        with tempfile.TemporaryDirectory() as tmp:
            for case in range(cases):
                state_dir = Path(tmp) / str(case)
                raw = {"topic": rng.choice(TOPICS), "budget": rng.randint(0, 20), "deadline": rng.choice(DEADLINES),
                       "start_time": "2026-11-02 09:00"}
                keys_before = 0
                for _ in range(3):  # 换故障和上限重复提交：覆盖续跑、重复执行
                    keys = rng.sample(FAULT_KEYS, rng.randint(0, 3))
                    faults = {k: rng.choice(FAULTS) for k in keys}
                    limits = {"max_tool_calls": rng.randint(1, 12)} if rng.random() < 0.3 else {}
                    req = normalize_request({**raw, "simulate": {"faults": faults}, "limits": limits}, catalog.fault_keys())
                    result = PlannerAgent(catalog, RunStore(state_dir), PlanStore(state_dir), policy_class(rng)).run(req)
                    with self.subTest(case=case):
                        check_result(self, result)
                        if result["repeat"]:
                            self.assertEqual(len(result["tool_calls"]), keys_before)  # 已完成的请求不再调用工具
                        keys_before = len(result["tool_calls"])
                        if result["saved_plan"]:
                            document = PlanStore(state_dir).get(result["request_key"])["plan"]
                            self.assertEqual(document["option"]["option_id"], result["plan"]["option_id"])
                            self.assertLessEqual(document["cost"]["total"], raw["budget"])

    def test_random_allowed_actions_never_break_the_invariants(self):
        self.run_fuzz(RandomPolicy, seed=20261031, cases=300)

    def test_garbage_decisions_never_break_the_invariants(self):
        self.run_fuzz(GarbagePolicy, seed=20261102, cases=300)


if __name__ == "__main__":
    unittest.main()
