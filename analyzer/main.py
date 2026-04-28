"""
主分析路径：从 scraped_content（ai_analyzed=0）逐条分析 → compliance_analysis。

run_analysis() 是包对外的总入口，编排顺序：
  1. 主分析（本模块的 _analyze_one）— 90% 流量走这里
  2. 字段补填 + 导航页回收
  3. 降级合成（fallback 子模块，可 --skip-fallback 跳过）
  4. Stage 3 整合去重（consolidation 子模块）
  5. 孤儿 scraped_content 回收
"""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta

import ai_client
from database import (
    get_unanalyzed_content,
    get_raw_result,
    mark_analyzed,
    delete_orphan_scraped,
    get_connection,
)
from utils import get_logger, parse_json_object, reg_hash

from ._shared import (
    PRODUCT_LIST,
    SYSTEM, PROMPT_TMPL,
    FALLBACK_SYSTEM, FALLBACK_PROMPT_TMPL,
    SYNTHESIS_WARNING,
    safe_print, truncate_smart,
)
from .values import build_analysis_values, insert_analysis_row
from .fallback import (
    run_fallback,
    requeue_navigation_failures,
    enforce_fallback_caps,
)
from .consolidation import run_consolidation
from .backfill import backfill_computed_fields

_log = get_logger("analyzer")

# 并发参数（Tier 1 = 1000 RPM ≈ 16 RPS，下面值留有余量）
_ANALYZE_WORKERS = 8   # 主分析每条 ~5s


def _analyze_one(idx: int, total: int, sc_row) -> str:
    """处理一条 scraped_content；返回 'ok'/'dup'/'fail'。"""
    raw       = get_raw_result(sc_row["raw_id"])
    title     = (raw["title"]      if raw else "") or ""
    url       = (raw["source_url"] if raw else "") or ""
    market    = (raw["market"]     if raw else "") or ""
    relevance = (raw["snippet"]    if raw else "") or ""
    full_text = truncate_smart(sc_row["full_text"] or "")

    if not full_text.strip():
        mark_analyzed(sc_row["id"])
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ 内容为空，跳过")
        return "fail"

    h      = reg_hash(title)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM compliance_analysis WHERE content_hash=? AND analysis_date>=?",
            (h, cutoff),
        ).fetchone():
            mark_analyzed(sc_row["id"])
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} → 重复，跳过")
            return "dup"

    # 上一轮 fallback 留下的合成内容若被重置 ai_analyzed=0 → 走降级 prompt
    is_synth = full_text.startswith("[Gemini synthesis]")
    if is_synth:
        tmpl, sys_prompt, extra_biz = FALLBACK_PROMPT_TMPL, FALLBACK_SYSTEM, SYNTHESIS_WARNING
    else:
        tmpl, sys_prompt, extra_biz = PROMPT_TMPL, SYSTEM, None

    prompt = tmpl.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        title=title or "（未知）",
        url=url or "（未知）",
        market=market or "（未知）",
        relevance=relevance or "（无说明）",
        scraped_text=full_text,
        product_list=PRODUCT_LIST,
    )

    try:
        text   = ai_client.call_json(prompt, system=sys_prompt)
        result = parse_json_object(text)
        if not result:
            mark_analyzed(sc_row["id"])
            _log.error("JSON parse fail scraped_id=%d", sc_row["id"])
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ JSON 解析失败")
            return "fail"

        if is_synth:
            enforce_fallback_caps(result)

        values = build_analysis_values(result, title, url, market, extra_biz=extra_biz)
        with get_connection() as conn:
            insert_analysis_row(conn, sc_row["id"], values, h)

        mark_analyzed(sc_row["id"])
        importance = values["importance"]
        products   = values["products"]
        _log.info("OK scraped_id=%d importance=%s products=%s",
                  sc_row["id"], importance, products)
        tag = "[不相关]" if products == "不相关" else products[:30]
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} → {importance}  {tag}")
        return "ok"

    except Exception as e:
        mark_analyzed(sc_row["id"])
        _log.error("FAIL scraped_id=%d: %s", sc_row["id"], e)
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ {e}")
        return "fail"


def run_analysis(skip_fallback: bool = False) -> tuple[int, int, int]:
    """
    返回 (analyzed_total, skipped_dup, failed_total)。
      • analyzed_total = 主分析成功 + 降级合成成功
      • skipped_dup    = 主分析阶段因 30 天窗口内重复跳过的条目
      • failed_total   = 主分析失败 + 降级合成失败

    skip_fallback：跳过对 scrape_status='失败' 条目的 Gemini 合成路径。
    用途：先用已抓到的数据看 Stage 3 收敛效果；后续可单独 retry 跑 fallback。

    整合去重的合并/删除数量打印到终端，但不计入返回 tuple。
    """
    def _maybe_fallback() -> tuple[int, int]:
        if skip_fallback:
            print("\n  ── 跳过降级合成（--skip-fallback）──")
            return 0, 0
        return run_fallback()

    unanalyzed = get_unanalyzed_content()
    if not unanalyzed:
        print("  没有待分析的内容。")
        backfill_computed_fields()
        requeue_navigation_failures()
        fa, ff = _maybe_fallback()
        mc, dc = run_consolidation()
        if mc > 0:
            print(f"\n  整合完成：合并 {mc} 组，删除 {dc} 条重复记录")
            deleted_orphans = delete_orphan_scraped()
            if deleted_orphans:
                print(f"  孤儿清理：移除 {deleted_orphans} 条 scraped_content")
        return fa, 0, ff

    total = len(unanalyzed)
    print(f"\n  共 {total} 条待分析（并发 {_ANALYZE_WORKERS}）\n")

    def _process(args):
        idx, sc_row = args
        return _analyze_one(idx, total, sc_row)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_ANALYZE_WORKERS, thread_name_prefix="analyze",
    ) as ex:
        results = list(ex.map(_process, list(enumerate(unanalyzed, 1))))

    analyzed = sum(1 for r in results if r == "ok")
    skipped  = sum(1 for r in results if r == "dup")
    failed   = sum(1 for r in results if r == "fail")

    print(f"\n  分析完成：成功 {analyzed}，重复跳过 {skipped}，失败 {failed}")

    backfill_computed_fields()
    requeue_navigation_failures()
    fa, ff = _maybe_fallback()

    print(f"\n  ── 整合去重 ──\n")
    mc, dc = run_consolidation()
    if mc > 0:
        print(f"\n  整合完成：合并 {mc} 组，删除 {dc} 条重复记录")
        deleted_orphans = delete_orphan_scraped()
        if deleted_orphans:
            print(f"  孤儿清理：移除 {deleted_orphans} 条 scraped_content")
    else:
        print("  无需合并，所有条目已是独立法规。")

    return analyzed + fa, skipped, failed + ff


if __name__ == "__main__":
    import sys
    run_analysis(skip_fallback="--skip-fallback" in sys.argv)
