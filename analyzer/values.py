"""
字段构造与 DB 写入：把 LLM 返回的 result dict 标准化成 compliance_analysis 行。

main 与 fallback 两条路径共用——保证两种来源的字段语义完全一致。
所有函数为纯函数（除 insert_analysis_row 写库外），便于单测。
"""
from __future__ import annotations

import json
from datetime import datetime

from classify import (
    normalize_products,
    compute_products_display,
    compute_market_tier,
    institution,
)

from ._shared import VALID_IMPORTANCE, VALID_DIMENSIONS


def coerce_str(v) -> str:
    """LLM 偶尔会把 string 字段返回成 list（如 business_impact 给数组）。
    统一兜底成字符串，避免 .strip() 报 'list has no attribute strip'。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n".join(coerce_str(x) for x in v if x is not None)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def build_analysis_values(
    result: dict,
    title: str,
    url: str,
    market: str,
    extra_biz: str | None,
) -> dict:
    # importance 由下游 llm_priority 在 reporter 阶段集中判定（写入 ai_priority）。
    # main analyzer 不再要求输出此字段——若 LLM 仍输出，存为兼容数据；否则 NULL。
    raw_importance = (result.get("importance") or "").strip()
    importance = raw_importance if raw_importance in VALID_IMPORTANCE else None

    products = normalize_products(result.get("affected_products", ""))

    # 五维业务影响坐标（Level 2）：过滤非枚举值，保留顺序去重
    raw_dims = result.get("business_dimensions") or []
    if not isinstance(raw_dims, list):
        raw_dims = []
    seen_dims: set[str] = set()
    business_dims: list[str] = []
    for d in raw_dims:
        if isinstance(d, str):
            d = d.strip().upper()
            if d in VALID_DIMENSIONS and d not in seen_dims:
                seen_dims.add(d)
                business_dims.append(d)

    dates_raw    = result.get("dates") or {}
    enforcements = dates_raw.get("enforcements") or []

    # Sanity check：effective 早于 publish 是 LLM 幻觉的明显标志
    # （常见 LLM 错把"母法规生效日"和"修订法案发布日"混合）
    # 经典案例：UK e-scooter pub=2026-04-29 eff=2026-01-01 — 差 4 个月不可能
    # 处理：异常时丢弃 effective（让其为 null），enforcements 数组里同样过滤
    pub_str = dates_raw.get("publish")
    eff_str = dates_raw.get("effective")
    eff_clean = eff_str
    if pub_str and eff_str:
        try:
            pub_d = pub_str[:10]
            eff_d = eff_str[:10]
            # 字符串日期比较（YYYY-MM-DD 字典序==时间序）
            if eff_d < pub_d:
                eff_clean = None  # 弃用错位的 effective
        except Exception:
            pass

    # enforcements 数组里 date < publish 的也清掉
    cleaned_enf = []
    if isinstance(enforcements, list):
        for e in enforcements:
            if isinstance(e, dict) and pub_str:
                d = (e.get("date") or "")[:10]
                if d and d < pub_str[:10]:
                    continue  # 强制日早于发布日 = 幻觉
            cleaned_enf.append(e)

    key_dates = json.dumps({
        "publish":            pub_str,
        "effective":          eff_clean,
        "enforcements":       cleaned_enf,
        "consultation_close": dates_raw.get("consultation_close"),
    }, ensure_ascii=False)

    worst_case = coerce_str(result.get("worst_case"))
    biz        = coerce_str(result.get("business_impact"))
    if extra_biz:
        biz = f"{extra_biz}\n{biz}".strip()

    affected_markets_str = coerce_str(result.get("affected_markets")) or market
    products_display       = compute_products_display(products)
    market_tier_val        = compute_market_tier(affected_markets_str)
    source_inst, source_lg = institution(url, market)

    return {
        "importance":          importance,
        "products":            products,
        "key_dates":           key_dates,
        "worst_case":          worst_case,
        "biz":                 biz,
        "affected_markets":    affected_markets_str,
        "products_display":    products_display,
        "market_tier":         market_tier_val,
        "source_inst":         source_inst,
        "source_lg":           source_lg,
        "requirement":         coerce_str(result.get("requirement")),
        "deadline":            result.get("deadline"),
        "url":                 url,
        "business_dimensions": business_dims,
    }


def insert_analysis_row(conn, scraped_id: int, v: dict, h: str) -> None:
    src_url = v.get("url") or ""
    sources = json.dumps([{"url": src_url}] if src_url else [], ensure_ascii=False)
    business_dims_json = json.dumps(v.get("business_dimensions") or [], ensure_ascii=False)
    conn.execute("""
        INSERT OR IGNORE INTO compliance_analysis
            (scraped_id, compliance_requirement, compliance_deadline,
             key_dates, action_items, impact_level, affected_products,
             affected_markets, worst_case_scenario, business_impact,
             business_dimensions, sources, content_hash, analysis_date,
             affected_products_display, market_tier,
             source_institution, source_language)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        scraped_id,
        v["requirement"],
        v["deadline"],
        v["key_dates"],
        json.dumps([], ensure_ascii=False),
        v["importance"],
        v["products"],
        v["affected_markets"],
        v["worst_case"],
        v["biz"],
        business_dims_json,
        sources,
        h,
        datetime.now().isoformat(),
        v["products_display"],
        v["market_tier"],
        v["source_inst"],
        v["source_lg"],
    ))
