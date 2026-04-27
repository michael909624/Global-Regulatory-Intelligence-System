"""
GRIS 共享工具：logging、JSON 容错解析、法规哈希。

集中管理是为了避免：
- 多模块各自调 logging.basicConfig 只有第一次生效
- _reg_hash 在 researcher / analyzer 重复定义并漂移
- JSON 解析容错代码到处粘贴
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

from config import LOGS_DIR


# ── logging ───────────────────────────────────────────────────────────────────

_LOGGERS_INITIALIZED: set[str] = set()


def get_logger(name: str) -> logging.Logger:
    """返回写入 logs/<name>.log 的独立 logger。同名 logger 只配置一次。"""
    if name in _LOGGERS_INITIALIZED:
        return logging.getLogger(name)
    os.makedirs(LOGS_DIR, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(
        os.path.join(LOGS_DIR, f"{name}.log"), encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    _LOGGERS_INITIALIZED.add(name)
    return logger


# ── 法规标题哈希 ──────────────────────────────────────────────────────────────


def reg_hash(title: str) -> str:
    """规范化标题后 md5。整个系统的去重契约依赖这一函数。"""
    return hashlib.md5((title or "").strip().lower().encode()).hexdigest()


# ── JSON 解析容错 ─────────────────────────────────────────────────────────────

_CITE_RE = re.compile(r"\[\d+\]")
_FENCE_RE = re.compile(r"```(?:json)?", re.I)


def _strip_wrapper(text: str) -> str:
    clean = _CITE_RE.sub("", text or "").strip()
    clean = _FENCE_RE.sub("", clean).strip().rstrip("`").strip()
    return clean


def _scan_balanced(text: str, opener: str, closer: str) -> str | None:
    """在 text 中查找平衡的 opener/closer 块；跳过字符串内字符。"""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _try_relaxed(fragment: str) -> Any:
    """容错：把字符串值内未转义的换行替换成 \\n。"""
    fixed = re.sub(
        r'"(?:[^"\\]|\\.)*"',
        lambda m: m.group(0).replace("\n", "\\n").replace("\r", "\\r"),
        fragment,
        flags=re.DOTALL,
    )
    return json.loads(fixed)


def parse_json_object(text: str) -> dict | None:
    """从 LLM 响应抽取单个 JSON 对象。"""
    fragment = _scan_balanced(_strip_wrapper(text), "{", "}")
    if not fragment:
        return None
    try:
        result = json.loads(fragment)
    except json.JSONDecodeError:
        try:
            result = _try_relaxed(fragment)
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, dict) else None


def parse_json_array(text: str) -> list | None:
    """从 LLM 响应抽取 JSON 数组。"""
    fragment = _scan_balanced(_strip_wrapper(text), "[", "]")
    if not fragment:
        return None
    try:
        result = json.loads(fragment)
    except json.JSONDecodeError:
        try:
            result = _try_relaxed(fragment)
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, list) else None
