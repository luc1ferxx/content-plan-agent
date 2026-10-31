"""只读业务数据：工具费用与耗时表、方案模板、合成素材库。费用/耗时一律查表，代码里不写死数字。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .common import DATA_DIR, add_minutes, load_json, num


@dataclass(frozen=True)
class Price:
    fee: int | float
    minutes: int
    fee_on_failure: int | float


class Catalog:
    def __init__(self, data_dir: Path = DATA_DIR):
        data_dir = Path(data_dir)
        self.tools = load_json(data_dir / "tools.json")["tools"]
        self.options = sorted(load_json(data_dir / "plan_options.json")["options"], key=lambda o: o["priority"])
        self.materials = load_json(data_dir / "materials.json")["materials"]

    def _spec(self, tool: str, mode: str | None) -> dict:
        spec = self.tools[tool]
        return spec["modes"][mode] if mode else spec

    def price(self, tool: str, mode: str | None = None) -> Price:
        spec = self._spec(tool, mode)
        return Price(spec["fee"], spec["minutes"], spec.get("fee_on_failure", 0))

    def label(self, tool: str, mode: str | None = None) -> str:
        name = self.tools[tool]["name"]
        return f"{name}（{self._spec(tool, mode)['name']}）" if mode else name

    def option(self, option_id: str) -> dict:
        for opt in self.options:
            if opt["option_id"] == option_id:
                return opt
        raise KeyError(option_id)

    def need_materials(self, option: dict) -> int:
        return self._spec("generate_script", option["script_mode"])["min_materials"]

    def fault_keys(self) -> set[str]:
        """可注入故障的目标：工具名，或 工具:模式（如 generate_script:premium）。"""
        keys = set()
        for tool, spec in self.tools.items():
            keys.add(tool)
            keys.update(f"{tool}:{mode}" for mode in spec.get("modes", {}))
        return keys

    def project(self, items, spent, clock: str) -> dict:
        """成本估算工具与执行器校验共用的算法：已花费 + 逐项 fee；模拟时钟 + 逐项 minutes。items 为 [(tool, mode)]。"""
        lines, fee, minutes = [], 0, 0
        for tool, mode in items:
            p = self.price(tool, mode)
            lines.append({"tool": tool, "mode": mode, "fee": p.fee, "minutes": p.minutes})
            fee += p.fee
            minutes += p.minutes
        return {"lines": lines, "spent_before": num(spent), "remaining_fee": num(fee), "remaining_minutes": minutes,
                "projected_total_fee": num(spent + fee), "projected_finish": add_minutes(clock, minutes)}
