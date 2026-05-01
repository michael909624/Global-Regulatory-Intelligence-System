"""
GRIS 共享工具：logging、JSON 容错解析、法规哈希、reg_id 归一化。

集中管理是为了避免：
- 多模块各自调 logging.basicConfig 只有第一次生效
- _reg_hash 在 researcher / analyzer 重复定义并漂移
- JSON 解析容错代码到处粘贴
- normalize_reg_id 跨模块 import,reporter ↔ consolidator ↔ analyzer 三角依赖
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


# ── reg_id 归一化 ──────────────────────────────────────────────────────────────
#
# 把模型自报的多种写法折叠到统一的聚类键。例：
#   "(EU) 2024/2847"             → "EU/2024/2847"
#   "Regulation (EU) 2024/2847"  → "EU/2024/2847"
#   "32024R2847" (CELEX)         → "EU/2024/2847"     ← 跨格式合并
#   "GB 17761"                   → "GB/17761"
#   "16 CFR Part 1273"           → "CFR/16-1273"
#   "UN R155"                    → "UN/R155"
#   "CRA" / "Cyber Resilience Act" → "ALIAS/CRA"
#
# 保守原则：只折叠"明确同一编号的不同写法"。"CRA" 与 "EU/2024/2847"
# 在 Stage 0 视作不同组(两者通过 ALIAS/EU 路径独立归一) —— 跨家族合并交给 Stage 3。
#
# 历史:这个函数原住 consolidator.py,但 reporter / analyzer.consolidation 都得
# import 它,形成跨层依赖。搬到 utils 后纯字符串处理函数,谁都能用。
#
# 循环导入注意:utils 导入 rules 会循环(rules.__init__ 导入 utils.get_logger)。
# 通过 lazy 加载在函数内 import 解决。

_STD_PREFIXES = ("EN", "IEC", "UL", "ISO", "JIS", "AS/NZS", "AIS", "ANSI", "CSA", "BS")

_REGID_RULES_LOADED = False
_ALIAS_MAP: tuple = ()
_ALIAS_TO_CELEX: tuple = ()


def _load_regid_rules() -> None:
    """首次调用时加载别名映射。延迟到调用时是为避免 utils ↔ rules 循环 import。"""
    global _REGID_RULES_LOADED, _ALIAS_MAP, _ALIAS_TO_CELEX
    if _REGID_RULES_LOADED:
        return
    from rules import load_pairs   # 延迟 import:rules 模块在导入时已经依赖 utils
    _ALIAS_MAP      = load_pairs("reg_aliases")
    _ALIAS_TO_CELEX = load_pairs("reg_alias_to_celex")
    _REGID_RULES_LOADED = True


def normalize_reg_id(raw: str | None) -> str | None:
    """归一化模型自报的 reg_id 到聚类键；返回 None 表示无法归类。

    设计:先尝试抽 CELEX / EU 编号(强信号),命中即归一。这样
    "32024R2847" / "32024R2847 Deadlines" / "32024R2847_Guidance"
    都归到同一 EU/2024/2847；CRA / AI Act 等已知别名也通过
    _ALIAS_TO_CELEX 反向映射到对应 CELEX。
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    _load_regid_rules()
    # 下划线在 \b 视角是 word char,会破坏词边界检测
    # ("CRA_Guidance" 里 \bCRA\b 不命中)。先转成空格再做正则。
    upper = s.upper().replace("_", " ")

    # CELEX → EU/YYYY/NNN(用 search 而非 match:含后缀/前缀的字符串也能抽出)
    # 支持 4 位或 5 位顺序号:32024R2847 / 32023R1542 / 32024R0900
    m = re.search(r"\b3(\d{4})[RLDC](\d{1,5})\b", upper)
    if m:
        return f"EU/{m.group(1)}/{int(m.group(2))}"

    # (EU) YYYY/NNN  /  Reg YYYY/NNN  /  Directive YYYY/NNN
    m = re.search(r"\(EU\)\s*(\d{4})/(\d+)", upper)
    if m:
        return f"EU/{m.group(1)}/{int(m.group(2))}"
    m = re.search(
        r"\b(?:REG|REGULATION|DIRECTIVE|DECISION|DELEGATED|IMPLEMENTING)"
        r"[A-Z\s\(\)]*?(\d{4})/(\d+)",
        upper,
    )
    if m:
        return f"EU/{m.group(1)}/{int(m.group(2))}"

    # 别名优先映射到对应 CELEX(让 "CRA" / "AI Act" 等条目和 CELEX 条目同组)
    for pat, celex_key in _ALIAS_TO_CELEX:
        if re.search(pat, upper):
            return celex_key

    # 美国 CFR
    m = re.search(r"\b(\d{1,3})\s*CFR\s*(?:PART\s*)?(\d+)", upper)
    if m:
        return f"CFR/{m.group(1)}-{m.group(2)}"
    m = re.search(r"\bCFR\s*PART\s*(\d+)", upper)
    if m:
        return f"CFR/0-{m.group(1)}"

    # 中国 GB / GB/T
    m = re.search(r"\bGB[/\s\-]?T?[\s\-]?(\d{4,6})", upper)
    if m:
        return f"GB/{m.group(1)}"

    # UN / UNECE Regulation
    m = re.search(
        r"\bUN\s*(?:ECE\s*)?R(?:EGULATION)?\s*(?:NO\.?)?\s*(\d+)",
        upper,
    )
    if m:
        return f"UN/R{m.group(1)}"

    # 国际标准(EN / IEC / UL / ISO / JIS / AS/NZS / AIS / ANSI / CSA / BS)
    for prefix in _STD_PREFIXES:
        pat = re.compile(rf"\b{re.escape(prefix)}[/\s\-]?(\d{{3,6}})", re.I)
        m = pat.search(upper)
        if m:
            key_prefix = prefix.upper().replace("/", "_")
            return f"{key_prefix}/{m.group(1)}"

    # 别名(CRA / AI Act / Battery Regulation / PSTI / RoHS / REACH / GDPR)
    for pat, alias in _ALIAS_MAP:
        if re.search(pat, upper):
            return f"ALIAS/{alias}"

    # 兜底:原值压缩作为弱聚类键(保留模型独有的奇怪编号)
    fallback = re.sub(r"[^\w\d/\-]", "", s.lower())
    if len(fallback) >= 4:
        return f"RAW/{fallback[:50]}"
    return None
