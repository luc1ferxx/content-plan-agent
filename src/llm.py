"""真实模型决策：DeepSeek（OpenAI 兼容接口），用标准库 urllib 调用。

模型在每一步根据观察结果选择下一步动作并给出依据；工具执行、记账、落盘和合法性校验由执行器负责。
调用失败或多次返回不合法动作时，执行器改用 MockPolicy，并在决策记录里写明原因。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .policy import InvalidDecision

DEFAULT_MODEL = "deepseek-v4-pro"

SYSTEM_PROMPT = """你是“内容活动方案规划 Agent”的决策模型，目标是在预算和截止时间内，完成并保存一份效果尽可能好的内容活动方案。
工具有四个：estimate_cost（成本估算）、search_materials（素材检索）、generate_script（脚本生成）、save_plan（方案保存）。
工具由执行器运行、记账和校验；你负责在每一步根据观察结果选择下一步动作。费用的单位是“模拟单位”（不是元），时间按模拟时钟计算。

每次你会收到当前状态的 JSON：event（刚发生的事件类型）、last_observation（观察到的结果）、budget / spent / deadline / clock、plan（当前计划与各步骤状态）、candidates（各方案）、retry（能否重试）、allowed_actions（当前允许的动作）。

你只能从 allowed_actions 中选一个动作，并只输出一个 JSON 对象，不要输出其他文字：
{"action": "plan 或 call_tool 或 retry 或 switch_option 或 stop", "option_id": "plan 和 switch_option 时填写，例如 P2", "tool": "call_tool 时填写工具名", "reason": "一句中文决策依据，引用观察到的具体数字或错误"}
allowed_actions 的写法：plan:P2 表示 {"action":"plan","option_id":"P2"}，switch_option:P2 同理，call_tool:search_materials 表示 {"action":"call_tool","tool":"search_materials"}。

决策原则：
1. priority 数字越小，方案效果越好。在满足预算和截止时间的前提下，选效果最好的方案。还不知道成本时选 priority 最小的方案，由成本估算工具给出费用和耗时。
2. candidates 里 is_current=true 的是当前正在执行的方案；feasible 表示该方案按当前已花费和时钟能否在预算和截止时间内完成，projected_total_fee 和 projected_finish 已计入已花费和可复用步骤。
3. 当前方案仍然 feasible、也没有新问题时，就继续执行下一步。预算刚好够用也算可行，不要为了留余量或省钱而主动降级。
4. 当前方案不再可行时，在 allowed_actions 里选效果最好的替代方案。
5. 已成功步骤的结果会被保留复用，不重复付费；失败调用可能已按 fee_on_failure 计费，已计入 spent。
6. 工具失败时看错误是否可重试：retry 在 allowed_actions 里时优先重试，以保住当前方案的效果；否则改选方案。
7. 部分成功时，判断现有结果够哪个方案使用。
8. 只有 allowed_actions 只剩 stop 时才停止，并在 reason 里说明缺口。"""


def brief(ctx: dict) -> dict:
    """发给模型的状态摘要。首次规划时不给成本预估：成本要由 estimate_cost 工具观察得到。"""
    planning = ctx["event"]["type"] == "plan"
    keys = (("option_id", "name", "priority", "script_mode", "need_materials", "blocked_by") if planning else
            ("option_id", "name", "priority", "script_mode", "need_materials", "is_current", "projected_total_fee",
             "projected_finish", "feasible", "blocked_by", "issues"))
    state = {
        "event": ctx["event"]["type"], "last_observation": ctx["event"]["text"], "topic": ctx["topic"],
        "budget": ctx["budget"], "spent": ctx["spent"], "clock": ctx["clock"], "deadline": ctx["deadline"],
        "minutes_left": ctx["minutes_left"], "materials_found": ctx["materials_found"], "plan": ctx["plan"],
        "retry": ctx["retry"], "candidates": [{k: c[k] for k in keys} for c in ctx["candidates"]],
        "allowed_actions": ctx["allowed_actions"],
    }
    if ctx.get("rejection"):
        state["previous_decision_rejected"] = ctx["rejection"]
    return state


def parse_json_object(text: str | None) -> dict:
    """取模型输出里的第一个 JSON 对象；前后多余的文字、代码块标记都忽略。"""
    start = (text or "").find("{")
    if start < 0:
        raise InvalidDecision("模型输出中没有 JSON 对象")
    try:
        return json.JSONDecoder().raw_decode(text, start)[0]
    except json.JSONDecodeError as exc:
        raise InvalidDecision(f"模型输出不是合法 JSON：{exc}") from None


class DeepSeekPolicy:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, base_url: str = "https://api.deepseek.com",
                 timeout: float = 90.0, transport=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or self._post
        self.name = f"llm:{model}"

    @classmethod
    def from_env(cls, model: str | None = None):
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if not key or any(ch.isspace() or ord(ch) < 32 for ch in key):  # 空值或含换行等非法字符都视为未配置
            return None
        return cls(key, model=model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
                   base_url=os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com")

    def decide(self, ctx: dict) -> dict:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(brief(ctx), ensure_ascii=False)},
            ],
        }
        resp = self.transport(f"{self.base_url}/chat/completions", body)
        decision = parse_json_object(resp["choices"][0]["message"]["content"])
        # 记下接口返回的响应 ID 和用量：记录里可以核对这次决策确实来自一次模型调用
        decision["model_call"] = {"response_id": resp.get("id"), "model": resp.get("model"),
                                  "total_tokens": (resp.get("usage") or {}).get("total_tokens")}
        return decision

    def _post(self, url: str, body: dict, attempts: int = 3) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(url, data=data, method="POST", headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError) as exc:  # HTTPError 是 URLError 的子类
                status = getattr(exc, "code", None)
                if attempt == attempts or (status is not None and status != 429 and status < 500):
                    raise
                time.sleep(attempt * 2)
        raise RuntimeError("未发出请求")
