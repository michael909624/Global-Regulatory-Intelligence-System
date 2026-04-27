"""
Prompts 加载器。

设计意图:把所有 AI 提示词从 .py 代码中抽出,放进 .txt 文件,
让产品/法务/合规同事可以直接编辑提示词而无需懂 Python。

用法:
    from prompts import load
    text = load("analyzer_system")                       # 静态文本
    text = load("analyzer_main").format(today=today, ...)  # 含占位符的模板
"""
from __future__ import annotations

import os
from functools import lru_cache

_BASE = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """读取 prompts/<name>.txt 的内容。结果会缓存,改文件需重启程序生效。"""
    path = os.path.join(_BASE, f"{name}.txt")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()
