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
import threading
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
    safe_print, truncate_smart, strip_injection_markers,
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

# ── 同 content_hash 串行化锁 ────────────────────────────────────────────────────
# 30 天去重的 SELECT existing → DELETE old → LLM → INSERT 跨多个 with 块，
# 8 worker 并发时若两条同 hash 同时进入：都看到旧合成版 → 都 DELETE → 都 INSERT，
# 最后只有一条因 UNIQUE INDEX 真生效，另一条被 OR IGNORE 静默吞但仍 mark_analyzed=1
# → "成功"日志骗人、库里少数据。用 hash 锁串行同 hash 的读改写。
#
# 内存影响：锁字典随见过的 hash 数量增长，单次 run 最多几千条法规无压力。
_HASH_LOCKS_GUARD = threading.Lock()
_HASH_LOCKS: dict[str, threading.Lock] = {}


def _hash_lock(h: str) -> threading.Lock:
    with _HASH_LOCKS_GUARD:
        lk = _HASH_LOCKS.get(h)
        if lk is None:
            lk = threading.Lock()
            _HASH_LOCKS[h] = lk
        return lk


def _check_dedup(h: str, cutoff: str, is_synth: bool):
    """30 天窗口去重检查。返回:
      "skip"     —— 同 hash 已分析过且不需替换,调用方应 mark_analyzed + 返回 'dup'
      None       —— 正常分析,无替换
      int(ca_id) —— 旧记录是合成版、本次是原文,LLM 成功后用结果替换该条

    例外:旧记录是合成版([Gemini synthesis])但本次是真原文 → 删旧让原文替换,
    否则真原文永远抢不过早一秒入库的合成版。
    """
    with get_connection() as conn:
        existing = conn.execute("""
            SELECT ca.id AS ca_id,
                   sc.full_text AS old_text
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            WHERE ca.content_hash=? AND ca.analysis_date>=?
            ORDER BY ca.id DESC
            LIMIT 1
        """, (h, cutoff)).fetchone()
    if not existing:
        return None
    old_is_synth = (existing["old_text"] or "").startswith("[Gemini synthesis]")
    if old_is_synth and not is_synth:
        return existing["ca_id"]
    return "skip"


def _llm_and_build(
    *, title: str, url: str, market: str, relevance: str,
    full_text: str, is_synth: bool, sc_id: int,
):
    """LLM 调用 → JSON 解析 → 字段构造。

    返回 (status, payload):
      ("ok",        values)   一切顺利,values 是 build_analysis_values 结果
      ("fail_temp", err_msg)  临时错误(API 限流/网络/SAFETY 屏蔽)→ 不 mark_analyzed
      ("fail_perm", err_msg)  永久错误(JSON 解析失败/字段构造失败)→ 应 mark_analyzed
    """
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
        # 主分析的字段抽取(requirement / dates / dimensions / products / markets)
        # 是下游所有阶段的依赖,准确度优先于成本 — 用 Gemini 3.1 Pro + 适度 thinking,
        # 把"结构化模板填空(flash 关 thinking)"升级为"理解 + 推断式抽取"。
        # 法规原文复杂(条文嵌套/多义务并列)时,Pro + thinking 显著优于 flash。
        text = ai_client.call_json(
            prompt, system=sys_prompt,
            model="gemini-3.1-pro-preview",
            thinking_budget=2048,
        )
    except Exception as e:
        _log.error("LLM call FAIL scraped_id=%d: %s", sc_id, e)
        return ("fail_temp", str(e))

    result = parse_json_object(text)
    if not result:
        _log.error("JSON parse fail scraped_id=%d", sc_id)
        return ("fail_perm", "JSON 解析失败")

    if is_synth:
        enforce_fallback_caps(result)

    try:
        values = build_analysis_values(result, title, url, market, extra_biz=extra_biz)
    except Exception as e:
        _log.error("build_values FAIL scraped_id=%d: %s", sc_id, e)
        return ("fail_perm", str(e))

    return ("ok", values)


def _persist(sc_id: int, values: dict, h: str, replace_id: int | None) -> bool:
    """删旧合成版(若有)+ INSERT 新分析 + 校验真写入。

    返回 True 表示成功;False 表示 INSERT OR IGNORE 因 UNIQUE 冲突静默吞,
    调用方应当 mark_analyzed 防死循环 + 返回 'dup'。
    """
    with get_connection() as conn:
        if replace_id is not None:
            conn.execute("DELETE FROM compliance_analysis WHERE id=?", (replace_id,))
            _log.info("REPLACE synth→raw scraped_id=%d ca=%d", sc_id, replace_id)
        insert_analysis_row(conn, sc_id, values, h)
        # INSERT OR IGNORE 在 UNIQUE 冲突时静默吞——必须校验真生效,
        # 否则 mark_analyzed 后这条 sc 就永久"成功但没数据"了。
        inserted = conn.execute(
            "SELECT 1 FROM compliance_analysis WHERE scraped_id=?", (sc_id,),
        ).fetchone()
    return bool(inserted)


def _analyze_one(idx: int, total: int, sc_row) -> str:
    """处理一条 scraped_content；返回 'ok'/'dup'/'fail'。

    失败语义分两类：
      • 内容/格式问题（空文本、JSON 解析失败、UNIQUE 冲突静默丢失）→ mark_analyzed
        让它退出待处理队列，避免下次再跑同样会失败的内容
      • 临时错误（API 限流、网络抖动、SAFETY 屏蔽）→ 不 mark_analyzed，下次重跑

    流程拆三段:_check_dedup → _llm_and_build → _persist;主函数纯编排 + print。
    同 hash 串行化整段:SELECT existing → LLM → DELETE+INSERT → 校验。
    LLM 调用在锁内看似拖慢,但 sqlite WAL 写本身就是串行的,且同 hash 极少撞,
    整体并发损失极小,换来的是数据一致性保证。
    DELETE 旧合成版推迟到 INSERT 同事务里——若 LLM 失败,旧合成版仍保留,
    不会出现"删了旧的、新的没写成、这条法规归零"的失败模式。
    """
    raw       = get_raw_result(sc_row["raw_id"])
    title     = (raw["title"]      if raw else "") or ""
    url       = (raw["source_url"] if raw else "") or ""
    market    = (raw["market"]     if raw else "") or ""
    relevance = (raw["snippet"]    if raw else "") or ""
    full_text = strip_injection_markers(truncate_smart(sc_row["full_text"] or ""))

    if not full_text.strip():
        mark_analyzed(sc_row["id"])
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ 内容为空，跳过")
        return "fail"

    from config import DEDUP_WINDOW_DAYS
    h        = reg_hash(title)
    cutoff   = (datetime.now() - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    is_synth = full_text.startswith("[Gemini synthesis]")

    with _hash_lock(h):
        decision = _check_dedup(h, cutoff, is_synth)
        if decision == "skip":
            mark_analyzed(sc_row["id"])
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} → 重复，跳过")
            return "dup"
        replace_id: int | None = decision   # None 或 ca_id
        if replace_id is not None:
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} ↻ 原文替换旧合成版（待 LLM 成功）")

        status, payload = _llm_and_build(
            title=title, url=url, market=market, relevance=relevance,
            full_text=full_text, is_synth=is_synth, sc_id=sc_row["id"],
        )
        if status == "fail_temp":
            # API 错误属临时性,不 mark_analyzed,下次重跑
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ {payload}（保留待重试）")
            return "fail"
        if status == "fail_perm":
            # JSON / 字段构造错重跑大概率仍失败,mark_analyzed 防死循环
            mark_analyzed(sc_row["id"])
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ {payload}")
            return "fail"
        values = payload

        inserted = _persist(sc_row["id"], values, h, replace_id)
        if not inserted:
            # 通常意味着同 hash 被另一条抢先写入(虽有 hash 锁,但跨进程/旧残留也会触发)
            _log.warning(
                "INSERT silently dropped (UNIQUE conflict) scraped_id=%d h=%s",
                sc_row["id"], h,
            )
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} → 同 hash 已有记录，跳过")
            mark_analyzed(sc_row["id"])
            return "dup"

    mark_analyzed(sc_row["id"])
    importance = values["importance"]
    products   = values["products"]
    _log.info("OK scraped_id=%d importance=%s products=%s",
              sc_row["id"], importance, products)
    tag = "[不相关]" if products == "不相关" else products[:30]
    safe_print(f"  [{idx:>3}/{total}] {title[:52]} → {importance}  {tag}")
    return "ok"


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
