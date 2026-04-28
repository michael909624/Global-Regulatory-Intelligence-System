"""
补填字段 + 重置不相关条目。

backfill_computed_fields  对历史记录重算 market_tier / affected_products_display，
                          为缺失的 source_institution / source_language 兜底
requeue_irrelevant        删除「不相关」分析并把对应 scraped_content 重置为待分析
                          （CLI: gris.py reanalyze 用）
"""
from __future__ import annotations

import json

from classify import (
    compute_market_tier,
    compute_products_display,
    institution,
)
from database import get_connection
from utils import get_logger

_log = get_logger("analyzer")


def backfill_computed_fields() -> None:
    """重算 market_tier / affected_products_display；为空 source_* 兜底。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, affected_markets, affected_products FROM compliance_analysis"
        ).fetchall()
        if rows:
            updates = [
                (
                    compute_market_tier(r["affected_markets"] or ""),
                    compute_products_display(r["affected_products"] or ""),
                    r["id"],
                )
                for r in rows
            ]
            conn.executemany(
                "UPDATE compliance_analysis "
                "SET market_tier = ?, affected_products_display = ? WHERE id = ?",
                updates,
            )
            print(f"  市场层级 & 产品显示已全部重算：{len(updates)} 条")

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ca.id, ca.sources, rs.source_url, rs.market
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            WHERE ca.source_institution IS NULL
        """).fetchall()

    if not rows:
        return

    print(f"\n  ── 补填来源机构：{len(rows)} 条历史记录 ──")

    for r in rows:
        market  = r["market"] or ""
        src_url = r["source_url"] or ""
        if r["sources"]:
            try:
                srcs = json.loads(r["sources"])
                if srcs and isinstance(srcs, list) and srcs[0].get("url"):
                    src_url = srcs[0]["url"]
            except Exception as e:
                _log.warning("sources parse fail id=%d: %s", r["id"], e)

        inst, lg = institution(src_url, market)
        with get_connection() as conn:
            conn.execute("""
                UPDATE compliance_analysis
                SET source_institution = ?, source_language = ?
                WHERE id = ?
            """, (inst, lg, r["id"]))

    print(f"  补填完成：{len(rows)} 条")


def requeue_irrelevant() -> int:
    """删除所有 '不相关' 分析并把对应 scraped_content 重置为待分析。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, scraped_id FROM compliance_analysis "
            "WHERE affected_products = '不相关'"
        ).fetchall()
        if not rows:
            print("  没有「不相关」条目需要重新分析。")
            return 0

        analysis_ids = [r["id"]         for r in rows]
        scraped_ids  = [r["scraped_id"] for r in rows]

        ph = ",".join("?" * len(analysis_ids))
        conn.execute(f"DELETE FROM compliance_analysis WHERE id IN ({ph})", analysis_ids)

        ph2 = ",".join("?" * len(scraped_ids))
        conn.execute(
            f"UPDATE scraped_content SET ai_analyzed = 0 WHERE id IN ({ph2})", scraped_ids
        )

    print(f"  已重置 {len(rows)} 条「不相关」条目，待重新分析。")
    return len(rows)
