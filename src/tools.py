"""四个工具：成本估算、素材检索、脚本生成、方案保存（合成实现，结果确定）。

工具只如实返回 成功 / 部分成功 / 失败，不做计划决策；计费与模拟时钟由执行器按工具费用表统一记账。
故障注入在真正执行前生效，保证“返回失败 = 没有产生副作用”（例如保存失败时方案库确实没写入）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .state import PlanStore


@dataclass
class ToolResult:
    ok: bool
    data: dict | None = None
    partial: bool = False
    error_code: str | None = None
    retryable: bool = False
    message: str = ""


def _premium(topic: str, materials: list[dict]) -> dict:
    beats = [("开场钩子", 3, "3 秒抛出「{topic}」的核心看点"),
             ("核心卖点", 10, "特写{title}，讲清一个最打动人的卖点"),
             ("使用场景", 12, "场景化演示，突出{tags}"),
             ("细节与口碑", 10, "用细节和真实反馈建立信任"),
             ("行动号召", 10, "引导点击/预约，强调活动时效")]
    shots = []
    for i, (purpose, seconds, line) in enumerate(beats):
        m = materials[i % len(materials)]
        shots.append({"shot": i + 1, "purpose": purpose, "seconds": seconds, "material_id": m["material_id"],
                      "visual": m["title"], "voiceover": line.format(topic=topic, title=m["title"], tags="、".join(m["tags"]))})
    return {"title": f"{topic}｜45 秒 AI 分镜短视频", "duration_sec": sum(s["seconds"] for s in shots),
            "shots": shots, "cta": f"点击了解「{topic}」活动详情"}


def _standard(topic: str, materials: list[dict]) -> dict:
    a, b = materials[0], materials[1]
    sections = [
        {"part": "开头", "seconds": 5, "material_id": a["material_id"], "copy": f"{topic}来了！先看{a['title']}"},
        {"part": "主体", "seconds": 18, "material_id": b["material_id"], "copy": f"借{b['title']}讲清亮点与使用场景"},
        {"part": "结尾", "seconds": 7, "material_id": a["material_id"], "copy": "主页链接参与活动，名额有限"},
    ]
    return {"title": f"{topic}｜30 秒短视频", "duration_sec": 30, "sections": sections, "cta": "主页链接参与活动"}


def _lite(topic: str, materials: list[dict]) -> dict:
    picked = materials[:3]
    return {"headline": f"{topic}：一图看懂活动亮点",
            "paragraphs": [f"亮点 {i + 1}：{m['title']}——{m['summary']}" for i, m in enumerate(picked)],
            "images": [m["material_id"] for m in picked], "cta": "点击原文链接参与活动"}


SCRIPT_BUILDERS = {"premium": _premium, "standard": _standard, "lite": _lite}


class ToolBox:
    def __init__(self, catalog: Catalog, plan_store: PlanStore, faults: dict | None = None):
        self.catalog = catalog
        self.plan_store = plan_store
        self.faults = dict(faults or {})

    def _fault(self, tool: str, mode: str | None):
        """返回 (命中的故障键, 故障类型)；tool:mode 精确匹配优先于 tool。"""
        for key in ([f"{tool}:{mode}"] if mode else []) + [tool]:
            if key in self.faults:
                return key, self.faults[key]
        return None, None

    def call(self, tool: str, mode: str | None, args: dict, history: list) -> ToolResult:
        """history：本次运行（含此前各轮）已发生的调用 [(tool, mode)]，让 fail_once 跨续跑也只失败一次。"""
        key, kind = self._fault(tool, mode)
        if kind == "fail_always":
            code = "STORAGE_READ_ONLY" if tool == "save_plan" else "SERVICE_UNAVAILABLE"
            return ToolResult(False, error_code=code, retryable=False,
                              message=f"[故障注入] {self.catalog.label(tool, mode)}不可用（{code}），本次没有产生任何结果")
        if kind == "fail_once":
            prior = sum(1 for t, m in history if t == tool and (":" not in key or m == mode))
            if prior == 0:
                return ToolResult(False, error_code="TIMEOUT", retryable=True,
                                  message=f"[故障注入] {self.catalog.label(tool, mode)}首次调用超时，可重试")
        handler = getattr(self, tool)
        return handler(mode=mode, partial=(kind == "partial"), **args)

    def estimate_cost(self, mode, partial, option_id: str, items: list, spent, clock: str) -> ToolResult:
        proj = self.catalog.project(items, spent, clock)
        return ToolResult(True, {"option_id": option_id, **proj},
                          message=f"{option_id} 预计合计 {proj['projected_total_fee']}，预计 {proj['projected_finish']} 完成")

    def search_materials(self, mode, partial, topic: str, need: int, limit: int = 5) -> ToolResult:
        hits = []
        for m in self.catalog.materials:
            score = sum(1 for tag in m["tags"] if tag in topic)
            if score:
                hits.append((score, m))
        hits.sort(key=lambda h: (-h[0], -h[1]["quality"], h[1]["material_id"]))
        found = [{k: m[k] for k in ("material_id", "title", "type", "tags", "summary")} for _, m in hits[:limit]]
        if partial and found:
            found = found[:max(1, min(len(found), need) - 1)]  # 注入“部分成功”：返回的条数比需要的少
        if not found:
            return ToolResult(False, error_code="NO_MATERIAL", retryable=False,
                              message=f"素材库中没有与「{topic}」相关的素材")
        data = {"topic": topic, "need": need, "found": len(found), "materials": found}
        if len(found) < need:
            return ToolResult(True, data, partial=True, message=f"部分成功：找到 {len(found)} 条，少于方案需要的 {need} 条")
        return ToolResult(True, data, message=f"找到 {len(found)} 条相关素材")

    def generate_script(self, mode, partial, topic: str, materials: list[dict]) -> ToolResult:
        spec = self.catalog.tools["generate_script"]["modes"][mode]
        if len(materials) < spec["min_materials"]:
            return ToolResult(False, error_code="INSUFFICIENT_MATERIALS", retryable=False,
                              message=f"{spec['name']}至少需要 {spec['min_materials']} 条素材，只提供了 {len(materials)} 条")
        script = SCRIPT_BUILDERS[mode](topic, materials)
        script["format"] = spec["name"]
        return ToolResult(True, script, message=f"已生成{spec['name']}")

    def save_plan(self, mode, partial, request_key: str, doc: dict) -> ToolResult:
        try:
            saved = self.plan_store.save(request_key, doc)
        except OSError as exc:
            return ToolResult(False, error_code="IO_ERROR", retryable=True, message=f"方案写入失败：{exc}")
        return ToolResult(True, saved, message=saved["message"])
