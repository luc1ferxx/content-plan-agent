"""运行记录自检：对 examples/ 下提交的实际输出（离线 Mock 与真实模型）逐份检查不变量。

记录是真实跑出来的结果，这里不重跑真实模型，只验证记录本身：预算、依赖、状态都对得上，真实模型记录确实由模型决策。
离线 Mock 是确定性的，所以还会重跑一遍，确认提交的 Mock 记录就是当前代码的实际输出。
"""
from __future__ import annotations

import glob
import os
import unittest

from invariants import check_result
from src.catalog import Catalog
from src.common import ROOT, load_json
from src.policy import MockPolicy
from src.run_examples import SCENARIOS, run_scenario

OUTPUTS = sorted(glob.glob(str(ROOT / "examples" / "**" / "outputs" / "*.json"), recursive=True))
REAL_DIRS = sorted(glob.glob(str(ROOT / "examples" / "real-*")))


def labels() -> list[str]:
    return [f"{name}.run{i}" if len(files) > 1 else name
            for name, _, files in SCENARIOS for i in range(1, len(files) + 1)]


class RecordInvariantTests(unittest.TestCase):
    def test_records_exist_for_every_scenario(self):
        for directory in [ROOT / "examples", *REAL_DIRS]:
            for label in labels():
                with self.subTest(directory=str(directory), label=label):
                    self.assertTrue(os.path.exists(os.path.join(directory, "outputs", f"{label}.json")))
                    self.assertTrue(os.path.exists(os.path.join(directory, "traces", f"{label}.md")))

    def test_every_record_keeps_invariants(self):
        self.assertTrue(OUTPUTS, "examples/ 下没有运行记录，请先运行 python -m src.run_examples")
        for path in OUTPUTS:
            with self.subTest(record=os.path.relpath(path, ROOT)):
                check_result(self, load_json(path))

    def test_real_model_records_are_decided_by_the_model(self):
        self.assertTrue(REAL_DIRS, "缺少真实模型记录，请运行 python -m src.run_examples --policy llm")
        for directory in REAL_DIRS:
            model = os.path.basename(directory).removeprefix("real-")
            for label in labels():
                with self.subTest(model=model, label=label):
                    r = load_json(os.path.join(directory, "outputs", f"{label}.json"))
                    self.assertEqual(r["policy"], f"llm:{model}")
                    self.assertEqual(r["mode"], "real_model")
                    for d in r["decisions"]:
                        self.assertIn(d["policy"], (f"llm:{model}", "guard"), d["trigger"])
                        self.assertTrue(d["reason"])
                        if d["policy"] != "guard":  # 每次决策都对应接口返回的一次响应
                            self.assertTrue(d["model_call"]["response_id"])
                            self.assertTrue(d["model_call"]["total_tokens"])

    def test_real_model_adjusts_to_different_tool_results(self):
        expected = {"over_budget": "estimate_rejected", "tool_failure": "tool_error", "partial_materials": "partial_result"}
        for directory in REAL_DIRS:
            for label, event_type in expected.items():
                with self.subTest(directory=os.path.basename(directory), label=label):
                    r = load_json(os.path.join(directory, "outputs", f"{label}.json"))
                    decision = next(d for d in r["decisions"] if d["event_type"] == event_type)
                    self.assertEqual(decision["action"], "switch_option")
                    self.assertEqual(r["status"], "completed")


    def test_real_records_actually_exercise_each_scenario(self):
        """故障场景必须真的碰到注入的故障：模型提前绕开的话，这份记录就证明不了验收点。"""
        expected_errors = {"tool_failure": "SERVICE_UNAVAILABLE", "tool_timeout_retry": "TIMEOUT",
                           "search_down": "SERVICE_UNAVAILABLE", "no_material": "NO_MATERIAL",
                           "resume.run1": "STORAGE_READ_ONLY"}
        for directory in REAL_DIRS:
            name = os.path.basename(directory)
            for label, code in expected_errors.items():
                with self.subTest(directory=name, label=label):
                    r = load_json(os.path.join(directory, "outputs", f"{label}.json"))
                    self.assertIn(code, [c["error_code"] for c in r["tool_calls"]])
            normal = load_json(os.path.join(directory, "outputs", "normal.json"))
            self.assertEqual((normal["status"], normal["plan"]["option_id"], normal["cost"]["spent"]), ("completed", "P1", 12))
            limited = load_json(os.path.join(directory, "outputs", "tool_limit.json"))
            self.assertEqual((limited["status"], limited["stop_code"]), ("incomplete", "TOOL_CALL_LIMIT"))


class MockRecordTests(unittest.TestCase):
    def test_committed_mock_records_match_a_fresh_run(self):
        catalog = Catalog()
        for name, _, files in SCENARIOS:
            for label, _, result in run_scenario(name, files, catalog, MockPolicy):
                with self.subTest(label=label):
                    committed = load_json(ROOT / "examples" / "outputs" / f"{label}.json")
                    self.assertEqual(committed, result)


if __name__ == "__main__":
    unittest.main()
