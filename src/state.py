"""持久化：运行状态库（每个请求一份，每次状态变化都落盘）与方案库（唯一的业务写操作，幂等）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .common import canonical_hash, load_json, write_json_atomic


class RunStore:
    """运行状态：按 request_key 一个文件。用于“重复执行直接返回”和“失败步骤续跑”。"""

    def __init__(self, root: Path):
        self.dir = Path(root) / "runs"

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def load(self, key: str) -> dict | None:
        path = self._path(key)
        return load_json(path) if path.exists() else None

    def save(self, state: dict) -> None:
        write_json_atomic(self._path(state["request_key"]), state)

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.exists():
            path.unlink()
            return True
        return False


class PlanStore:
    """方案库（JSON 文件）。按 request_key 幂等：同一请求只有一份方案；内容相同不重复写，内容变化版本 +1。"""

    def __init__(self, root: Path):
        self.path = Path(root) / "plans.json"

    def _load(self) -> dict:
        return load_json(self.path) if self.path.exists() else {"plans": {}}

    def get(self, key: str) -> dict | None:
        return self._load()["plans"].get(key)

    def count(self) -> int:
        return len(self._load()["plans"])

    def save(self, key: str, doc: dict) -> dict:
        data = self._load()
        content_hash = canonical_hash(doc)
        existing = data["plans"].get(key)
        if existing and existing["content_hash"] == content_hash:
            return {"result": "duplicate_unchanged", "plan_id": existing["plan_id"], "version": existing["version"],
                    "message": "同一请求的方案已存在且内容一致，未重复保存"}
        now = datetime.now().isoformat(timespec="seconds")
        record = {
            "plan_id": doc["plan_id"],
            "request_key": key,
            "version": existing["version"] + 1 if existing else 1,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            "content_hash": content_hash,
            "plan": doc,
        }
        data["plans"][key] = record
        write_json_atomic(self.path, data)
        result = "updated" if existing else "created"
        message = "同一请求的方案已更新（版本 +1），未新建方案" if existing else "方案已保存"
        return {"result": result, "plan_id": record["plan_id"], "version": record["version"], "message": message}
