"""通用工具：路径、JSON 读写、模拟时钟、.env 读取。只依赖标准库。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / ".runtime"
TIME_FORMAT = "%Y-%m-%d %H:%M"


def load_json(path):
    """兼容 UTF-8（含 BOM）与 UTF-16（PowerShell 5.1 Out-File 默认）编码的 JSON 文件。"""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    return json.loads(text)


def write_json_atomic(path, data) -> None:
    """先写临时文件再替换，避免写一半留下损坏的状态/方案库。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def canonical_hash(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_time(text: str) -> datetime:
    """解析 'YYYY-MM-DD HH:MM'，格式不对抛 ValueError。"""
    return datetime.strptime(text.strip(), TIME_FORMAT)


def fmt_time(dt: datetime) -> str:
    return dt.strftime(TIME_FORMAT)


def add_minutes(text: str, minutes: int) -> str:
    return fmt_time(parse_time(text) + timedelta(minutes=minutes))


def minutes_between(start: str, end: str) -> int:
    """end - start 的分钟数，可为负。"""
    return int((parse_time(end) - parse_time(start)).total_seconds() // 60)


def num(x):
    """数字归一：消除小数预算带来的浮点误差，整数值的 float 转成 int（10.0 → 10），便于展示与核对。"""
    if isinstance(x, float):
        x = round(x, 6)
        if x.is_integer():
            return int(x)
    return x


def is_number(x) -> bool:
    if isinstance(x, bool):
        return False
    return isinstance(x, int) or (isinstance(x, float) and math.isfinite(x))


def load_dotenv(path) -> None:
    """极简 .env 读取：KEY=VALUE，忽略注释；不覆盖已存在的环境变量。"""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
