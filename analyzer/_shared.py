"""
analyzer 包共享：常量、提示词、文本截断、并发安全打印。

放这里的标准：被 ≥2 个子模块共用，且属于"分析"业务域内部细节
（不应被包外代码 import）。下划线开头表示包内私有。
"""
from __future__ import annotations

import re
import threading

import prompts
from config import PRODUCT_LINES

# ── 文本长度参数 ──────────────────────────────────────────────────────────────
_MAX_TEXT   = 80_000   # 单条原文最长 80k 字符（约 20k tokens）
_HEAD_CHARS = 50_000   # 长文档头部保留
_TAIL_CHARS = 30_000   # 长文档尾部保留（含强制日 / 罚则 / 附录，关键不可丢）


def truncate_smart(text: str) -> str:
    """长文档智能截断：≤80k 完整保留；>80k 取头 50k + 尾 30k。
    法规典型结构里，关键日期 / 罚则 / 附录常在尾部，单纯前置截断会丢这些。
    """
    if not text:
        return ""
    if len(text) <= _MAX_TEXT:
        return text
    head = text[:_HEAD_CHARS]
    tail = text[-_TAIL_CHARS:]
    omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
    return f"{head}\n\n[...中部 {omitted:,} 字符已省略，保留头部适用范围 + 尾部罚则/强制日...]\n\n{tail}"


# ── prompt injection 防御 ─────────────────────────────────────────────────────
# 真实环境下抓回的法规原文偶尔会含恶意 prompt injection（被入侵的网站、
# 攻击者投放的伪法规页等）。LLM 看到 "ignore previous instructions" 等指令时
# 有不可忽略的中招概率，会按攻击者指令输出"不相关"使真法规漏召回。
#
# 这里的防御不是"完美抵抗 injection"——根治需要 LLM 厂商侧的 system prompt 加固。
# 我们只剥离已知模式的明显注入标记，让喂给 LLM 的内容尽量干净。
_INJECTION_PATTERNS = [
    re.compile(r"system\s+override\s*:.*", re.IGNORECASE | re.DOTALL),
    re.compile(r"ignore\s+(?:the\s+above|all\s+previous|previous\s+instructions).*",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"\[admin\s+note[^\]]*\].*", re.IGNORECASE | re.DOTALL),
    re.compile(r"the\s+user\s+has\s+updated\s+(?:the\s+)?rules.*",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"output\s+only\s+the\s+following\s+(?:json|response).*",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"actual\s+classification\s+per\s+agency\s+memo.*",
               re.IGNORECASE | re.DOTALL),
]


def strip_injection_markers(text: str) -> str:
    """剥离已知 prompt injection 模式段落（从 marker 开始到末尾）。
    设计上偏保守：只删命中明显模式的部分，宁可漏删也不冤删合规原文。
    """
    if not text:
        return text
    out = text
    for pat in _INJECTION_PATTERNS:
        out = pat.sub("", out)
    return out


# ── 业务枚举 ──────────────────────────────────────────────────────────────────
VALID_IMPORTANCE = {"🔴", "🟡", "🟢"}
PRODUCT_LIST     = "、".join(PRODUCT_LINES)

# 八维 L3 业务影响枚举（与 business_scope.txt 一致）
# L2（售前/售中/售后）由 L3 派生，不让模型重复填写。
VALID_DIMENSIONS = {"RD", "PROD", "CERT", "IMPORT", "RETAIL", "USE", "ENFORCE", "EOL"}


# ── 公共提示词 ────────────────────────────────────────────────────────────────
# 业务影响坐标（business_scope）作为公共判定基础，注入到所有 analyzer 系统提示。
# Level 2：让 researcher / analyzer / consolidation 共享同一套相关性判定标准。
BUSINESS_SCOPE = prompts.load("business_scope")

SYSTEM = prompts.load("analyzer_system").format(
    product_list=PRODUCT_LIST,
    business_scope=BUSINESS_SCOPE,
)
PROMPT_TMPL          = prompts.load("analyzer_main")
FALLBACK_SYSTEM      = SYSTEM + prompts.load("analyzer_fallback_extension")
FALLBACK_PROMPT_TMPL = prompts.load("analyzer_fallback")

SYNTHESIS_WARNING = "⚠️ 原文抓取失败，此条目基于 AI 合成，请人工核实后再使用。"


# ── 并发打印锁 ────────────────────────────────────────────────────────────────
_print_lock = threading.Lock()


def safe_print(msg: str) -> None:
    """并发场景下避免输出交错。"""
    with _print_lock:
        print(msg, flush=True)
