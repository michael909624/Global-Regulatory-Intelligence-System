"""
priority.py — 时间窗口判定 + LLM 失败兜底打分。

【架构变更（治本重构 → 启发式下放）】
本模块由 470 行启发式（L1/L2 关键词 + 强保护 + 5 维度评分）精简为 ~80 行
机械工具——噪音 / 过程阶段 / 媒体白名单 / 跨主题词 / 市场层级等业务规则
**全部下放给 LLM 三件套**用 few-shot 范本判定:
  • analyzer/llm_triage.py     — scraper 前批量预筛 raw 标题（覆盖原 is_noise）
  • analyzer/llm_priority.py   — reporter 前末端终审（覆盖原 is_process_stage 等）
  • prompts/llm_triage_system.txt / llm_priority_system.txt 含完整范本

本模块剩余职责:
  1. in_time_window(row)  — 时间窗口判定（datetime.now() ± 90/365 天）
  2. score_fallback(row)  — LLM 失败时的简化兜底评分（来源真实度 + 时间窗 + 市场）

reporter._filter_and_repaint 调用顺序:
  Stage 1 时间窗粗筛: in_time_window
  Stage 2 LLM 终审:   优先读 ai_priority 字段（已写回 DB）
  Stage 3 兜底:       LLM 字段缺失时调 score_fallback
"""
from __future__ import annotations

import json
from datetime import datetime

from classify import compute_market_tier


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


# ── 公共 API ────────────────────────────────────────────────────────────────

def in_time_window(row, today=None) -> bool:
    """是否在时间窗口内（近 90 天发布 OR 未来 12 月生效 OR 已生效持续期内）。

    用 datetime.now() 实时锚定,不依赖 LLM 训练知识里的过时日期。
    """
    if today is None:
        today = datetime.now()
    pub, eff, enfs = _parse_key_dates(_row_get(row, "key_dates"))

    pub_th         = today.timestamp() - RECENT_PUBLISH_DAYS * 86400
    eff_far_max    = today.timestamp() + NEAR_FUTURE_EFFECTIVE_DAYS * 86400
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


def score_fallback(row, today=None) -> tuple[int, str] | None:
    """LLM 失败时的简化兜底评分。

    返回 (score, suggested_impact) 或 None（不建议进 P0）。
    评分仅 3 维度（来源真实度 + 时间窗口 + 市场）。
    建议 impact_level: 兜底情境下统一保守标 🟡（无法精准判 L1/L2）。

    设计: 兜底是"保守不漏球"——尽可能让真法规进 P0,让用户事后审。
    市场源用 classify.compute_market_tier(市场字符串) → 0..99,统一一份市场表。
    """
    if today is None:
        today = datetime.now()

    is_synth = bool(_row_get(row, "is_synth", 0))
    s_source = 20 if not is_synth else 10
    in_win   = in_time_window(row, today=today)
    s_time   = 25 if in_win else 10

    # classify.compute_market_tier 返回 0..99
    # （0=全球 / 1=欧盟 / 2=单一欧国 / 3=北美 / 4=美联邦 / 5=美州 / 6=加拿大联邦 / 7=加省 / 8=澳新 / 99=其他）
    # 折成三档供兜底打分用:
    market_str = _row_get(row, "affected_markets", "") or ""
    cls_tier = compute_market_tier(market_str)
    if cls_tier <= 2:
        s_market = 15      # 主流: 全球 / 欧盟 / 单一欧国
    elif cls_tier <= 5:
        s_market = 12      # 北美 / 美联邦 / 美州
    else:
        s_market = 5       # 加拿大 / 澳新 / 其他

    score = s_source + s_time + s_market   # 满分 60

    # 兜底门槛: ≥45 进 P0; < 45 不进
    if score < 45:
        return None

    return score, "🟡"
