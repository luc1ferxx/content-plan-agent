"""请求校验与归一化。格式错误抛 ValueError（CLI 退出码 2）；缺预算/截止时间等业务缺失交给 Agent 返回 needs_input。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .common import canonical_hash, fmt_time, is_number, num, parse_time

FAULT_KINDS = ("fail_once", "fail_always", "partial")
LIMIT_NAMES = ("max_tool_calls", "max_model_turns")
REQUIRED = (
    ("topic", "topic（活动主题）"),
    ("budget", "budget（预算，模拟单位）"),
    ("deadline", "deadline（截止时间 YYYY-MM-DD HH:MM）"),
)


@dataclass
class PlanRequest:
    topic: str | None
    budget: int | float | None
    deadline: str | None
    start_time: str
    request_id: str | None = None
    faults: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)

    def missing_inputs(self) -> list[str]:
        return [label for name, label in REQUIRED if getattr(self, name) is None]

    @property
    def request_key(self) -> str | None:
        """幂等键只由业务内容决定（主题 + 预算 + 截止时间），不含开始时间、request_id、上限与故障注入开关。"""
        if self.missing_inputs():
            return None
        return canonical_hash({"topic": self.topic, "budget": self.budget, "deadline": self.deadline})[:16]

    def to_dict(self) -> dict:
        return {"topic": self.topic, "budget": self.budget, "deadline": self.deadline,
                "start_time": self.start_time, "request_id": self.request_id,
                "faults": dict(self.faults), "limits": dict(self.limits)}


def _time_field(raw: dict, name: str) -> str | None:
    value = raw.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串，格式 'YYYY-MM-DD HH:MM'")
    try:
        return fmt_time(parse_time(value))
    except ValueError:
        raise ValueError(f"{name} 格式应为 'YYYY-MM-DD HH:MM'") from None


def _limits(raw: dict) -> dict:
    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("limits 必须是对象")
    for name, value in limits.items():
        if name not in LIMIT_NAMES:
            raise ValueError(f"未知的上限 {name}，可选：{LIMIT_NAMES}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"limits.{name} 必须是正整数")
    return dict(limits)


def normalize_request(raw, valid_fault_keys=None, now: datetime | None = None) -> PlanRequest:
    if not isinstance(raw, dict):
        raise ValueError("输入必须是 JSON 对象")
    topic = raw.get("topic")
    if topic is not None and not isinstance(topic, str):
        raise ValueError("topic 必须是字符串")
    topic = topic.strip() if topic and topic.strip() else None
    if topic is not None:
        try:
            topic.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("topic 含有无法编码的字符") from None

    budget = raw.get("budget")
    if budget == "":
        budget = None
    if budget is not None:
        if not is_number(budget):
            raise ValueError("budget 必须是数字（模拟单位）")
        if budget < 0:
            raise ValueError("budget 不能为负数")
        budget = num(budget)

    deadline = _time_field(raw, "deadline")
    start_time = _time_field(raw, "start_time") or fmt_time(now or datetime.now())
    if deadline and parse_time(deadline) <= parse_time(start_time):
        raise ValueError("deadline 必须晚于 start_time")

    simulate = raw.get("simulate") or {}
    if not isinstance(simulate, dict):
        raise ValueError("simulate 必须是对象")
    faults = simulate.get("faults") or {}
    if not isinstance(faults, dict):
        raise ValueError('simulate.faults 必须是对象，如 {"generate_script:premium": "fail_always"}')
    for key, kind in faults.items():
        if valid_fault_keys is not None and key not in valid_fault_keys:
            raise ValueError(f"未知的故障注入目标 {key}，可选：{sorted(valid_fault_keys)}")
        if kind not in FAULT_KINDS:
            raise ValueError(f"故障类型只能是 {FAULT_KINDS}，收到 {kind!r}")
        if kind == "partial" and key != "search_materials":
            raise ValueError("partial（部分成功）只适用于 search_materials")
    request_id = raw.get("request_id")
    if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
        raise ValueError("request_id 必须是字符串或整数")
    return PlanRequest(topic, budget, deadline, start_time, request_id, dict(faults), _limits(raw))
