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
    importance = (result.get("importance") or "🟢").strip()
    if importance not in VALID_IMPORTANCE:
        importance = "🟢"

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
    key_dates    = json.dumps({
        "publish":            dates_raw.get("publish"),
        "effective":          dates_raw.get("effective"),
        "enforcements":       enforcements if isinstance(enforcements, list) else [],
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
