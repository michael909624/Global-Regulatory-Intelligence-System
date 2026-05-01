"""
priority.py — 机械工具 + LLM 失败兜底。

【架构变更（治本重构后）】
本模块由 470 行启发式（L1/L2 关键词 + 强保护 + 5 维度评分）
精简为 ~150 行机械工具 + 简化兜底。

判定主体已移至 `analyzer/llm_priority.py`（reporter 渲染前调用，
写回 compliance_analysis.ai_priority / ai_level / ai_reason）。

本模块剩余职责：
  1. is_noise(row)        — 噪音剔除（单次召回 / 媒体 / 跨主题误命中）
  2. in_time_window(row)  — 时间窗口判定（datetime.now() ± 90/365 天）
  3. score_fallback(row)  — LLM 失败时的简化兜底评分

reporter._filter_and_repaint 调用顺序：
  Stage 1 机械粗筛: is_noise + in_time_window
  Stage 2 LLM 终审: 优先读 ai_priority 字段（已写回 DB）
  Stage 3 兜底:    LLM 字段缺失时调 score_fallback
"""
from __future__ import annotations

import json
from datetime import datetime


# ── 噪音模式（单次执法/个案/媒体/跨主题误命中 — 直接剔除）────────────────────
# 这些是抽象的"明显噪音"模式，不针对具体法规——是规则泛化。
_NOISE_TITLE_PATTERNS = (
    # 单次执法案例
    " civil penalty for ", " agreed to pay ", " sentenced for ",
    " disqualified ", " 被判 ", " 被罚 ", " disqualified from ",
    # 单次产品警告（CPSC Warns Consumers 模式）
    "warns consumers to", "warns consumers about",
    "stop using", "immediately stop using",
    # 媒体/博客/指南文章
    "tout ce qu'il faut savoir", "inthezone guide", " | guide ",
    "新浪财经", "搜狐", "腾讯网", "网易新闻", "凤凰网",
    "些 \"e-asy\" rules", "easy rules to follow",
    "新闻发布会",
)
_RECALL_HINT = "recall"
_REGULATION_HINTS = ("regulation", "standard", "act ", "directive", "rule",
                     "法规", "标准", "条例", "法案", "EPR")

# 跨主题误命中：含这些词且 不含 LEV/电池 关键词 → 视为跨主题噪音
# 治本扩展：加医疗器械/食品/化妆品/农药 等明显跨领域类
_CROSS_TOPIC_HINTS = (
    # 数据隐私/电信跨主题
    "consumer privacy", "ccpa", "data protection", "data privacy",
    "telecommunications", "telecom", "broadcasting",
    # 化学品（仅纯废物管理跨主题；RoHS 指令本身跟 LEV 直接相关，不归跨主题，
    # 让时间窗口判定它是否进 P0 即可）
    "hazardous waste management",
    # 医疗器械（明显非 LEV）
    "medical device", "医疗器械", "pharmaceut", "药品", "药物",
    "in vitro diagnostic",
    # 食品/化妆品（明显非 LEV）
    "food safety", "食品安全", "网络食品", "食品销售",
    "cosmetic", "化妆品", "personal care",
    # 农业/农药/烟草
    "agricult", "pesticide", "农药", "化肥", "tobacco", "烟草",
    # 航空/海事/枪支（除非含锂电池语境）
    "firearms", "枪支", "ammunition", "弹药",
)
_LEV_BATTERY_HINTS = (
    # 电池/锂电
    "battery", "batteries", "lithium", "锂电",
    # LEV 整机
    "e-bike", "e-scooter", "e-mobility", "micromobility",
    "electric bicycle", "electric scooter", "电动自行车", "电动滑板",
    "kick scooter", "personal mobility",
    # IoT / 联网产品（PSTI 等覆盖 LEV 类联网产品）
    "iot", "connected product", "connectable product",
    "product security", "psti", "cyber resilience",
    "联网产品", "智能产品",
)


# ── 主流市场（仅用于排序）────────────────────────────────────────────────────
_TIER1_MARKETS = ("欧盟", "美国", "中国", "日本", "英国", "韩国",
                  "EU", "US ", "USA", "China", "Japan", "UK ", "Korea",
                  "United States", "United Kingdom")
_TIER2_MARKETS = ("德国", "法国", "意大利", "西班牙", "加拿大", "澳大利亚",
                  "Germany", "France", "Italy", "Spain", "Canada", "Australia",
                  "加州", "California", "New York", "Texas", "Florida")


# ── 时间窗口（datetime.now() 实时锚定）──────────────────────────────────────
RECENT_PUBLISH_DAYS        = 90    # 近 90 天发布
NEAR_FUTURE_EFFECTIVE_DAYS = 365   # 未来 12 月生效
EXPIRED_TOLERANCE_DAYS     = 180   # 已过强制日 6 月内仍算"持续生效"


def _row_get(row, key, default=None):
    try:
        v = row[key]
        return v if v is not None else default
    except (IndexError, KeyError):
        return default


def _parse_date(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _parse_key_dates(kd_json):
    try:
        kd = json.loads(kd_json or "{}")
    except Exception:
        return None, None, []
    if not isinstance(kd, dict):
        return None, None, []
    pub = _parse_date(kd.get("publish"))
    eff = _parse_date(kd.get("effective"))
    enfs = []
    for e in (kd.get("enforcements") or []):
        if isinstance(e, dict):
            d = _parse_date(e.get("date"))
            if d:
                enfs.append(d)
    return pub, eff, enfs


def _market_tier(markets):
    if not markets:
        return 3
    for m in _TIER1_MARKETS:
        if m in markets:
            return 1
    for m in _TIER2_MARKETS:
        if m in markets:
            return 2
    return 3


# ── 公共 API ────────────────────────────────────────────────────────────────

def is_noise(row) -> bool:
    """标题命中明显噪音模式（单次召回/媒体/跨主题误命中）→ True，剔除。

    抽象规则，不针对具体名字。命中即直接剔除（不进任何 sheet）。
    """
    title = _row_get(row, "title", "") or ""
    if not title:
        return False
    low = title.lower()
    if any(p in low for p in _NOISE_TITLE_PATTERNS):
        return True
    # "recall" 单独命中且无标准/法规对照词 → 视为单次召回案例
    if _RECALL_HINT in low and not any(h in low for h in _REGULATION_HINTS):
        return True
    # 跨主题误命中：标题含跨主题关键词但不含 LEV/电池 → 跟我们业务无关
    if any(c in low for c in _CROSS_TOPIC_HINTS):
        if not any(h in low for h in _LEV_BATTERY_HINTS):
            return True
    return False


# 过程阶段法规标记词（无确定生效日 → 不进 P0）
_PROCESS_STAGE_PATTERNS_EN = (
    " (proposed)", "proposed rule", "proposed regulation",
    "notice of intent", "notice of proposed",
    "call for evidence", "consultation launch", "launches consultation",
    "draft regulation", "discussion paper", "green paper",
    "is consulting", "seeks comments",
)
_PROCESS_STAGE_PATTERNS_CN = (
    "拟修订", "拟议", "拟出台", "拟实施", "审议中",
    "通过内阁审议", "提请审议", "送审稿",
    "征求意见", "公开征求", "意见征集", "公开咨询",
)


def is_process_stage(row) -> bool:
    """法规仍在立法过程中（拟议/审议/咨询）→ True，不该进 P0。

    判别原则：title 含过程阶段标记词 AND key_dates.effective 为 null
    （已立法且有强制日的不算过程阶段——例如已发到联邦公报有 effective
    date 的法案不再 drop）。

    注意：用户业务目标是"已生效或即将生效的法规"，过程阶段不属于。
    LLM judge prompt 已有反例，这里再加规则兜底防遗漏。
    """
    title = _row_get(row, "title", "") or ""
    title_cn = _row_get(row, "title_cn", "") or ""
    text_low = (title + " " + title_cn).lower()

    hit = (any(p in text_low for p in _PROCESS_STAGE_PATTERNS_EN)
           or any(p in (title + title_cn) for p in _PROCESS_STAGE_PATTERNS_CN))
    if not hit:
        return False

    # 检查 key_dates.effective — 有具体生效日就放行（已立法）
    pub, eff, enfs = _parse_key_dates(_row_get(row, "key_dates"))
    if eff:
        return False  # 有具体生效日 → 已立法
    return True


def in_time_window(row, today=None) -> bool:
    """是否在时间窗口内（近 90 天发布 OR 未来 12 月生效 OR 已生效持续期内）。

    用 datetime.now() 实时锚定，不依赖 LLM 训练知识里的过时日期。
    """
    if today is None:
        today = datetime.now()
    pub, eff, enfs = _parse_key_dates(_row_get(row, "key_dates"))

    pub_th = today.timestamp() - RECENT_PUBLISH_DAYS * 86400
    eff_far_max = today.timestamp() + NEAR_FUTURE_EFFECTIVE_DAYS * 86400
    eff_recent_min = today.timestamp() - EXPIRED_TOLERANCE_DAYS * 86400

    if pub and pub.timestamp() >= pub_th and pub <= today:
        return True
    if eff and today.timestamp() <= eff.timestamp() <= eff_far_max:
        return True
    if eff and eff_recent_min <= eff.timestamp() < today.timestamp():
        return True
    for d in enfs:
        if today.timestamp() <= d.timestamp() <= eff_far_max:
            return True
    return False


def market_sort_tier(row) -> int:
    """返回市场 tier（1/2/3），仅用于 reporter 排序。"""
    return _market_tier(_row_get(row, "affected_markets", "") or "")


def score_fallback(row, today=None) -> tuple[int, str] | None:
    """LLM 失败时的简化兜底评分。

    返回 (score, suggested_impact) 或 None（不建议进 P0）。
    评分仅 3 维度（来源真实度 + 时间窗口 + 市场），不再做关键词分类。
    建议的 impact_level：根据是否合成 + 时效 → 🔴/🟡，不再有 🟢。

    设计：兜底是"保守不漏球"——尽可能让真法规进 P0，让用户事后审。
    """
    if today is None:
        today = datetime.now()

    is_synth = bool(_row_get(row, "is_synth", 0))
    s_source = 20 if not is_synth else 10
    in_win = in_time_window(row, today=today)
    s_time = 25 if in_win else 10
    tier = market_sort_tier(row)
    s_market = {1: 15, 2: 12, 3: 5}[tier]

    score = s_source + s_time + s_market   # 满分 60

    # 兜底门槛：≥45 进 P0；< 45 不进
    if score < 45:
        return None

    # 颜色建议：保守按 🟡（兜底情境无法精准判 L1/L2，给中等保守色）
    return score, "🟡"


# ── 向后兼容：旧 API 保留，内部委托新函数 ───────────────────────────────────
# reporter._filter_and_repaint 改造期间可能仍调用旧名字；改造完成后可删。

def score(row, today=None):
    """[已废弃] 旧主判定函数。新代码请用 is_noise + in_time_window + score_fallback。

    返回 (score, tags, reason, new_impact) 或 None（噪音/窗口外）。
    """
    if is_noise(row):
        return None
    if not in_time_window(row, today=today):
        return None
    res = score_fallback(row, today=today)
    if res is None:
        return None
    s, imp = res
    return s, ["[fallback]"], f"score={s}", imp


def tier_of(score_value):
    """[已废弃] 旧分层映射。"""
    return "P0" if score_value >= 45 else "P1"


def classify_rows(rows, today=None):
    """[已废弃] 旧批量分层函数。"""
    buckets = {"P0": [], "P1": [], "P2": [], "NOISE": []}
    for r in rows:
        result = score(r, today=today)
        if result is None:
            buckets["NOISE"].append((r, "noise/out-of-window"))
            continue
        s, tags, reason, ni = result
        bucket = "P0" if s >= 45 else "P1"
        buckets[bucket].append((r, s, tags, reason, ni))
    for k in ("P0", "P1", "P2"):
        buckets[k].sort(key=lambda t: -t[1])
    return buckets
