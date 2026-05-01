"""
业务规则加载器。

设计意图:把"业务数据型"硬编码（法规别名映射、市场层级、产品排序、导航关键词）
从 .py 代码抽出,放进 rules/*.txt 文件,让法务/合规同事可以直接编辑而无需懂 Python。

跟 prompts/__init__.py 同模式:lru_cache + utf-8 文本。

用法:
    from rules import load_lines, load_pairs

    # 单列规则(一行一个值)
    nav_keywords = load_lines("navigation_titles")
    # → ("press release", "site map", "glossary", ...)

    # 映射规则(一行一对 key => value)
    aliases = load_pairs("reg_alias_to_celex")
    # → (("CRA|CYBER\\s+RESILIENCE\\s+ACT", "EU/2024/2847"), ...)
    for pat, celex in aliases:
        if re.search(pat, title_upper):
            return celex

文件格式约定(对所有 .txt):
  • 每行一条规则
  • # 开头的整行 = 注释,忽略
  • 空行忽略
  • UTF-8 编码

load_pairs 安全网:
  若某行 key 是非法正则(用户写错反斜杠),log warning 并跳过该行,
  其他合法行照常加载——不会让流水线启动失败。
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

from utils import get_logger

_log = get_logger("rules")
_BASE = os.path.dirname(os.path.abspath(__file__))


def _read_lines(name: str) -> list[str]:
    """读 rules/<name>.txt,跳过空行和 # 注释。"""
    path = os.path.join(_BASE, f"{name}.txt")
    if not os.path.exists(path):
        raise FileNotFoundError(f"规则文件不存在: {path}")
    out: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


@lru_cache(maxsize=None)
def load_lines(name: str) -> tuple[str, ...]:
    """单列规则:每行一个值。返回 tuple 防止调用方误改污染缓存。"""
    return tuple(_read_lines(name))


@lru_cache(maxsize=None)
def load_pairs(name: str, sep: str = "=>") -> tuple[tuple[str, str], ...]:
    """映射型规则:每行 'key <sep> value'。

    返回 tuple of (key, value) 元组——保序(文件行序即迭代序),
    且不可被调用方误改。

    若 key 是正则,启动时验证 re.compile 通过,不通过的行 log warning 并跳过,
    保证调用方拿到的每个 key 都可以安全地 re.search。
    """
    out: list[tuple[str, str]] = []
    for line in _read_lines(name):
        if sep not in line:
            _log.warning("rules/%s.txt 行格式错误(缺 %r): %s", name, sep, line[:60])
            continue
        key, _, value = line.partition(sep)
        key, value = key.strip(), value.strip()
        if not key or not value:
            _log.warning("rules/%s.txt key/value 为空: %s", name, line[:60])
            continue
        try:
            re.compile(key)
        except re.error as e:
            _log.warning("rules/%s.txt 正则编译失败 %r: %s", name, key, e)
            continue
        out.append((key, value))
    return tuple(out)
