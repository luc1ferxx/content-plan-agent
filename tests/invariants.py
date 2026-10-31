"""运行结果的不变量：测试里的每次运行和 examples/ 下提交的每份记录都要过这一组检查。"""
from __future__ import annotations

ACTION_BEFORE_ESTIMATE = ("plan", "switch_option", "retry")


def check_result(test, r: dict) -> None:
    if r["status"] == "needs_input":
        test.assertEqual(r["tool_calls"], [])
        test.assertEqual(r["cost"]["spent"], 0)
        test.assertTrue(r["incomplete_reason"])
        return
    budget, calls = r["cost"]["budget"], r["tool_calls"]
    steps = {s["step_id"]: s for s in r["steps"]}
    # 1. 预算核对：费用合计 = 账本之和 = 各次调用费用之和；任何时刻累计都不超过预算
    test.assertEqual(r["cost"]["spent"], sum(x["fee"] for x in r["cost"]["ledger"]))
    running = 0
    for c in calls:
        running += c["fee"]
        test.assertEqual(c["spent_after"], running)
        test.assertLessEqual(c["spent_after"], budget, f"第 {c['seq']} 次调用后超预算")
    test.assertEqual(r["cost"]["spent"], running)
    # 2. 模拟时钟连续：每次调用都从上一次结束的时刻开始
    clock = r["request"]["start_time"]
    for c in calls:
        test.assertEqual(c["clock_before"], clock)
        clock = c["clock_after"]
    test.assertEqual(r["time"]["clock"], clock)
    # 3. 依赖：每次调用前，它依赖的步骤都已有成功/部分成功的调用；估算依赖必须是“通过”
    for c in calls:
        for dep in steps[c["step_id"]]["depends_on"]:
            done = [d for d in calls if d["step_id"] == dep and d["seq"] < c["seq"] and d["outcome"] != "error"]
            test.assertTrue(done, f"{c['step_id']} 在依赖 {dep} 完成前被调用")
            if steps[dep]["kind"] == "estimate":
                test.assertEqual(steps[dep]["verdict"], "pass")
    # 4. 状态真实：状态与该步骤最后一次调用结果一致；未执行的步骤没有调用；尝试次数、费用逐一对得上
    for s in r["steps"]:
        mine = [c for c in calls if c["step_id"] == s["step_id"]]
        test.assertEqual(len(mine), s["attempts"])
        test.assertEqual(s["fee_charged"], sum(c["fee"] for c in mine))
        expected_last = {"succeeded": "success", "partial": "partial", "failed": "error"}.get(s["status"])
        if expected_last:
            test.assertEqual(mine[-1]["outcome"], expected_last, s["step_id"])
        else:
            test.assertIn(s["status"], ("superseded", "blocked"))
            test.assertEqual(mine, [])
    # 5. 决策驱动：每次工具调用之前都有一次决策选中了它
    for c in calls:
        before = [d for d in r["decisions"] if d["after_call"] == c["seq"] - 1]
        test.assertTrue(before, f"第 {c['seq']} 次调用之前没有决策")
        last = before[-1]
        if c["tool"] == "estimate_cost":
            test.assertIn(last["action"], ACTION_BEFORE_ESTIMATE)
        elif last["action"] != "retry":
            test.assertEqual((last["action"], last["tool"]), ("call_tool", c["tool"]))
    # 6. 完成 = 当前计划每一步都满足、在截止时间内、方案确实写入；未完成必须给出原因
    if r["status"] == "completed":
        test.assertIsNone(r["stop_code"])
        test.assertGreaterEqual(r["time"]["minutes_left"], 0)
        test.assertIsNotNone(r["saved_plan"])
        test.assertTrue(r["plan_document_saved"])
        for s in r["plan"]["steps"]:
            test.assertIn(s["status"], ("succeeded", "partial"))
    else:
        test.assertTrue(r["incomplete_reason"])
        test.assertTrue(r["stop_code"])
