"""
Analyzer：基于真实抓取原文做合规分析。

Pipeline：
  1. 主分析：从 scraped_content（ai_analyzed=0）逐条分析 → compliance_analysis
  2. 导航页回收：把「直抓得到却被判'不相关'」且字数极少的条目转入降级合成队列
  3. 降级合成：抓取失败的条目用 Gemini grounding 合成内容后分析（带 ⚠️ 标记）
  4. 整合去重：按 URL + 法规编号确定性合并；同域同主题候选交给 Gemini 二次判断
  5. 补填：重算 market_tier / affected_products_display / source_*
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta

import ai_client
import authority
import prompts
from config import PRODUCT_LINES
from consolidator import normalize_reg_id
from database import (
    get_connection,
    get_unanalyzed_content,
    get_raw_result,
    mark_analyzed,
    delete_orphan_scraped,
)
from utils import get_logger, parse_json_array, parse_json_object, reg_hash
from classify import (
    normalize_products,
    compute_products_display,
    compute_market_tier,
    institution,
)

_log = get_logger("analyzer")

_MAX_TEXT     = 80_000   # 单条原文最长 80k 字符（约 20k tokens）
_HEAD_CHARS   = 50_000   # 长文档头部保留
_TAIL_CHARS   = 30_000   # 长文档尾部保留（含强制日 / 罚则 / 附录，关键不可丢）
_VALID_IMPORTANCE = {"🔴", "🟡", "🟢"}
_PRODUCT_LIST = "、".join(PRODUCT_LINES)


def _truncate_smart(text: str) -> str:
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


# Fallback 启发式：判断一条 scrape 失败的条目是否值得跑 grounded 合成。
# 分三档过滤掉明显幻觉条目，保留真实法规候选。
_REGULATORY_KEYWORDS_RE = re.compile(
    r"召回|公告|通告|决议|指南|通知|公示|法令|法律|条例|规定|命令|"
    r"认定|批复|行政处罚|强制|实施|修改单|修订|"
    r"\brecall\b|\border\b|\bnotice\b|\bguidance\b|\bdecree\b|"
    r"\bregulation\b|\bdirective\b|\brule\b|\bdecision\b|\bcircular\b|"
    r"\bamendment\b|\bimplementing\b|\bdelegated\b|"
    r"通達|改正|告示|お知らせ|決定|"
    r"고시|공고|결정|개정",
    re.IGNORECASE,
)

# 占位 / 幻觉编号特征——看到就跳过 fallback
_PLACEHOLDER_PATTERNS_RE = re.compile(
    r"\bXXXX?\b|\bTBD\b|\bTBA\b|placeholder|"
    r"\(EU\)\s+\d{4}/XXX|YYYY/NNN",
    re.IGNORECASE,
)


def _should_fallback(row) -> bool:
    """决定一条 scrape 失败的条目是否值得调 grounded 合成。
    A 档：reg_id 有效 → 必跑
    B 档：无 reg_id 但 title 含监管关键词 → 跑
    C 档：占位编号 / 模糊 title → 跳过（标"需人工"）
    """
    title = (row["title"] or "").strip()
    reg_id = ""
    try:
        reg_id = (row["reg_id"] or "").strip()
    except (IndexError, KeyError):
        pass

    # 档 A：reg_id 有效
    if reg_id and reg_id.lower() not in ("null", "none", "n/a", ""):
        return True

    # 占位 / 幻觉编号 → 跳过
    if _PLACEHOLDER_PATTERNS_RE.search(title):
        return False

    # 档 B：title 含监管关键词
    if _REGULATORY_KEYWORDS_RE.search(title):
        return True

    # 档 C：模糊 title → 跳过
    return False

# 并发参数（Tier 1 = 1000 RPM ≈ 16 RPS，下面值留有余量）
_ANALYZE_WORKERS     = 8   # 主分析每条 ~5s
_FALLBACK_WORKERS    = 5   # 降级合成含 grounded 调用，更慢
_CONSOLIDATE_WORKERS = 3   # consolidation LLM 调用

# print 锁——避免并发时输出交错
_print_lock = threading.Lock()


def _safe_print(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ── 提示词(从 prompts/ 加载) ────────────────────────────────────────────────
# 业务影响坐标（business_scope）作为公共判定基础，注入到所有 analyzer 系统提示。
# Level 2：让 researcher / analyzer / consolidation 共享同一套相关性判定标准。

_BUSINESS_SCOPE = prompts.load("business_scope")

_SYSTEM = prompts.load("analyzer_system").format(
    product_list=_PRODUCT_LIST,
    business_scope=_BUSINESS_SCOPE,
)
_PROMPT_TMPL          = prompts.load("analyzer_main")
_FALLBACK_SYSTEM      = _SYSTEM + prompts.load("analyzer_fallback_extension")
_FALLBACK_PROMPT_TMPL = prompts.load("analyzer_fallback")

_SYNTHESIS_WARNING = "⚠️ 原文抓取失败，此条目基于 AI 合成，请人工核实后再使用。"

# 八维 L3 业务影响枚举（与 business_scope.txt 一致）
# L2（售前/售中/售后）由 L3 自动派生（见 _L2_OF），不让模型重复填写。
_VALID_DIMENSIONS = {"RD", "PROD", "CERT", "IMPORT", "RETAIL", "USE", "ENFORCE", "EOL"}

_L2_OF: dict[str, str] = {
    "RD":      "售前",
    "PROD":    "售前",
    "CERT":    "售前",
    "IMPORT":  "售中",
    "RETAIL":  "售中",
    "USE":     "售中",
    "ENFORCE": "售后",
    "EOL":     "售后",
}


def _coerce_str(v) -> str:
    """LLM 偶尔会把 string 字段返回成 list（如 business_impact 给数组）。
    统一兜底成字符串，避免 .strip() 报 'list has no attribute strip'。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n".join(_coerce_str(x) for x in v if x is not None)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


# ── Gemini 调用 ───────────────────────────────────────────────────────────────
# 分析层：temperature=0(ai_client 默认)保证同一原文的分类/字段提取稳定可复现。

def _call_gemini(prompt: str, *, system: str = _SYSTEM) -> str:
    """无 grounding 调用，并请求 JSON 响应。"""
    return ai_client.call_json(prompt, system=system)


def _gemini_grounding_fetch(title: str, url: str, market: str, relevance: str) -> str | None:
    """grounded fetch：用 Google Search 搜法规内容。仅用于降级合成路径。"""
    prompt = prompts.load("grounding_fetch").format(
        title=title, url=url, market=market, relevance=relevance,
    )
    try:
        # 内容补全也用 temp=0,只想要事实性引用,不要发挥
        text, _ = ai_client.call_grounded(
            prompt,
            system=prompts.load("grounding_fetch_system"),
            temperature=0.0,
            top_p=None,
            return_sources=False,
        )
        text = text.strip()
        return text or None
    except Exception as e:
        _log.warning("Gemini fetch failed for %s: %s", title[:40], e)
        return None


# ── 字段构造助手 ──────────────────────────────────────────────────────────────

def _build_analysis_values(
    result: dict,
    title: str,
    url: str,
    market: str,
    extra_biz: str | None,
) -> dict:
    importance = (result.get("importance") or "🟢").strip()
    if importance not in _VALID_IMPORTANCE:
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
            if d in _VALID_DIMENSIONS and d not in seen_dims:
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

    worst_case = _coerce_str(result.get("worst_case"))
    biz        = _coerce_str(result.get("business_impact"))
    if extra_biz:
        biz = f"{extra_biz}\n{biz}".strip()

    affected_markets_str = _coerce_str(result.get("affected_markets")) or market
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
        "requirement":         _coerce_str(result.get("requirement")),
        "deadline":            result.get("deadline"),
        "url":                 url,
        "business_dimensions": business_dims,
    }


def _insert_analysis_row(conn, scraped_id: int, v: dict, h: str) -> None:
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


# ── 单条主分析（并发 worker）─────────────────────────────────────────────────

def _analyze_one(idx: int, total: int, sc_row) -> str:
    """处理一条 scraped_content；返回 'ok'/'dup'/'fail'。"""
    raw       = get_raw_result(sc_row["raw_id"])
    title     = (raw["title"]      if raw else "") or ""
    url       = (raw["source_url"] if raw else "") or ""
    market    = (raw["market"]     if raw else "") or ""
    relevance = (raw["snippet"]    if raw else "") or ""
    full_text = _truncate_smart(sc_row["full_text"] or "")

    if not full_text.strip():
        mark_analyzed(sc_row["id"])
        _safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ 内容为空，跳过")
        return "fail"

    h      = reg_hash(title)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM compliance_analysis WHERE content_hash=? AND analysis_date>=?",
            (h, cutoff),
        ).fetchone():
            mark_analyzed(sc_row["id"])
            _safe_print(f"  [{idx:>3}/{total}] {title[:52]} → 重复，跳过")
            return "dup"

    # 上一轮 fallback 留下的合成内容若被重置 ai_analyzed=0 → 走降级 prompt
    is_synth = full_text.startswith("[Gemini synthesis]")
    if is_synth:
        tmpl, sys_prompt, extra_biz = _FALLBACK_PROMPT_TMPL, _FALLBACK_SYSTEM, _SYNTHESIS_WARNING
    else:
        tmpl, sys_prompt, extra_biz = _PROMPT_TMPL, _SYSTEM, None

    prompt = tmpl.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        title=title or "（未知）",
        url=url or "（未知）",
        market=market or "（未知）",
        relevance=relevance or "（无说明）",
        scraped_text=full_text,
        product_list=_PRODUCT_LIST,
    )

    try:
        text   = _call_gemini(prompt, system=sys_prompt)
        result = parse_json_object(text)
        if not result:
            mark_analyzed(sc_row["id"])
            _log.error("JSON parse fail scraped_id=%d", sc_row["id"])
            _safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ JSON 解析失败")
            return "fail"

        if is_synth:
            _enforce_fallback_caps(result)

        values = _build_analysis_values(result, title, url, market, extra_biz=extra_biz)
        with get_connection() as conn:
            _insert_analysis_row(conn, sc_row["id"], values, h)

        mark_analyzed(sc_row["id"])
        importance = values["importance"]
        products   = values["products"]
        _log.info("OK scraped_id=%d importance=%s products=%s",
                  sc_row["id"], importance, products)
        tag = "[不相关]" if products == "不相关" else products[:30]
        _safe_print(f"  [{idx:>3}/{total}] {title[:52]} → {importance}  {tag}")
        return "ok"

    except Exception as e:
        mark_analyzed(sc_row["id"])
        _log.error("FAIL scraped_id=%d: %s", sc_row["id"], e)
        _safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ {e}")
        return "fail"


# ── 主入口 ────────────────────────────────────────────────────────────────────

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
        return _run_fallback()

    unanalyzed = get_unanalyzed_content()
    if not unanalyzed:
        print("  没有待分析的内容。")
        _backfill_computed_fields()
        _requeue_navigation_failures()
        fa, ff = _maybe_fallback()
        mc, dc = _run_consolidation()
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

    _backfill_computed_fields()
    _requeue_navigation_failures()
    fa, ff = _maybe_fallback()

    print(f"\n  ── 整合去重 ──\n")
    mc, dc = _run_consolidation()
    if mc > 0:
        print(f"\n  整合完成：合并 {mc} 组，删除 {dc} 条重复记录")
        deleted_orphans = delete_orphan_scraped()
        if deleted_orphans:
            print(f"  孤儿清理：移除 {deleted_orphans} 条 scraped_content")
    else:
        print("  无需合并，所有条目已是独立法规。")

    return analyzed + fa, skipped, failed + ff


# ── 导航页回收（直接抓到却被判'不相关'且正文极少 → 转入降级合成）──────────────
#
# 用字符数而非 word_count：中文段落无空格，word_count 度量在 CJK 下不稳定。
# 800 字符约等于半页 A4 正文，覆盖中英文导航页的典型规模。
_NAV_PAGE_CHAR_THRESHOLD = 800


def _requeue_navigation_failures() -> int:
    """把误判为'不相关'的导航页/索引页转入降级合成队列。"""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ca.id, rs.id AS raw_id, rs.title
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            WHERE ca.affected_products = '不相关'
              AND (sc.full_text IS NULL OR sc.full_text NOT LIKE '[Gemini synthesis]%')
              AND (sc.full_text IS NULL OR length(sc.full_text) < ?)
        """, (_NAV_PAGE_CHAR_THRESHOLD,)).fetchall()

        if not rows:
            return 0

        for r in rows:
            conn.execute("DELETE FROM compliance_analysis WHERE id = ?", (r["id"],))
            conn.execute(
                "UPDATE raw_search_results SET scrape_status = '失败' WHERE id = ?",
                (r["raw_id"],),
            )
            _log.info("REQUEUE nav-failure raw_id=%d title=%s",
                      r["raw_id"], (r["title"] or "")[:40])

    print(f"\n  ── 导航页检测：{len(rows)} 条已转入 Gemini 合成队列 ──")
    return len(rows)


# ── 降级合成（Gemini grounding）───────────────────────────────────────────────


def _enforce_fallback_caps(result: dict) -> None:
    """合成路径不变量：importance 不超过 🟡，importance_note 必含「数据来源：AI 合成」。"""
    if result.get("importance") == "🔴":
        result["importance"] = "🟡"
    note = (result.get("importance_note") or "").strip()
    if "数据来源：AI 合成" not in note:
        note = (note + " ｜ 数据来源：AI 合成").strip(" ｜")
    result["importance_note"] = note


def _fallback_one(idx: int, total: int, row) -> str:
    """对一条 scrape_status='失败' 的条目跑 grounded 合成 + 分析；返回 'ok'/'fail'。"""
    title     = (row["title"]      or "").strip()
    url       = (row["source_url"] or "").strip()
    market    = (row["market"]     or "").strip()
    relevance = (row["snippet"]    or "").strip()
    h         = reg_hash(title)

    synth_text = _gemini_grounding_fetch(title, url, market, relevance)
    if not synth_text:
        _mark_manual(row["id"])
        _safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ 合成失败 → 需人工")
        return "fail"

    prompt = _FALLBACK_PROMPT_TMPL.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        title=title or "（未知）",
        url=url or "（未知）",
        market=market or "（未知）",
        relevance=relevance or "（无说明）",
        scraped_text=_truncate_smart(synth_text),
        product_list=_PRODUCT_LIST,
    )

    try:
        text   = _call_gemini(prompt, system=_FALLBACK_SYSTEM)
        result = parse_json_object(text)
        if not result:
            _mark_manual(row["id"])
            _safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ JSON 解析失败 → 需人工")
            return "fail"

        _enforce_fallback_caps(result)
        values = _build_analysis_values(
            result, title, url, market, extra_biz=_SYNTHESIS_WARNING,
        )
        with get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO scraped_content
                    (raw_id, full_text, content_type, scrape_date,
                     word_count, truncated, ai_analyzed)
                VALUES (?, ?, 'unknown', ?, ?, 0, 1)
            """, (
                row["id"],
                f"[Gemini synthesis]\n{synth_text}",
                datetime.now().isoformat(),
                len(synth_text.split()),
            ))
            sc_id = cur.lastrowid
            _insert_analysis_row(conn, sc_id, values, h)
            conn.execute(
                "UPDATE raw_search_results SET scrape_status='需人工' WHERE id=?",
                (row["id"],),
            )

        importance = values["importance"]
        products   = values["products"]
        _log.info("SYNTH raw_id=%d importance=%s products=%s",
                  row["id"], importance, products)
        tag = "[不相关]" if products == "不相关" else products[:28]
        _safe_print(f"  [{idx:>3}/{total}] {title[:52]} → {importance}  {tag}  ⚠️")
        return "ok"

    except Exception as e:
        _mark_manual(row["id"])
        _log.error("SYNTH FAIL raw_id=%d: %s", row["id"], e)
        _safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ {e} → 需人工")
        return "fail"


def _run_fallback() -> tuple[int, int]:
    with get_connection() as conn:
        failed_rows = conn.execute("""
            SELECT rs.* FROM raw_search_results rs
            WHERE rs.scrape_status = '失败'
              AND NOT EXISTS (
                  SELECT 1 FROM compliance_analysis ca
                  WHERE ca.content_hash = rs.content_hash
              )
        """).fetchall()

    if not failed_rows:
        return 0, 0

    # 启发式过滤：跳过明显幻觉条目（占位编号 / 模糊 title）
    eligible = [r for r in failed_rows if _should_fallback(r)]
    skipped  = [r for r in failed_rows if not _should_fallback(r)]

    if skipped:
        with get_connection() as conn:
            for r in skipped:
                conn.execute(
                    "UPDATE raw_search_results SET scrape_status='需人工' WHERE id=?",
                    (r["id"],),
                )
        print(f"\n  ── Fallback 启发式过滤："
              f"{len(failed_rows)} 失败 → {len(eligible)} 跑合成 / {len(skipped)} 跳过（标需人工）──")
        _log.info("FALLBACK_FILTER total=%d eligible=%d skipped=%d",
                  len(failed_rows), len(eligible), len(skipped))

    if not eligible:
        return 0, 0

    failed_rows = eligible
    total = len(failed_rows)
    print(f"\n  ── 降级处理（Gemini 合成，并发 {_FALLBACK_WORKERS}）：{total} 条 ──\n")

    def _process(args):
        idx, row = args
        return _fallback_one(idx, total, row)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_FALLBACK_WORKERS, thread_name_prefix="fallback",
    ) as ex:
        results = list(ex.map(_process, list(enumerate(failed_rows, 1))))

    analyzed = sum(1 for r in results if r == "ok")
    failed   = sum(1 for r in results if r == "fail")

    print(f"\n  降级完成：合成分析 {analyzed}，失败 {failed}（均已标记需人工复核）")
    return analyzed, failed


def _mark_manual(raw_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_search_results SET scrape_status='需人工' WHERE id=?",
            (raw_id,),
        )


# ── 整合去重（Stage 3）──────────────────────────────────────────────────────
#
# 两阶段策略：
#   Pass 1：按 normalize_reg_id 跨域确定性合并（不调 LLM）
#           —— 替代旧的 _dedup_by_url；同一 reg_id 的不同切片直接合并
#   Pass 2：剩余条目按主题前缀分组，调 LLM 做语义合并
#           —— 不再按域名分组；prompt 偏向积极合并
#
# 选 keeper 的优先级：
#   1. 非合成条目优先（合成路径带 ⚠️，可信度低）
#   2. 来源域名权威分高优先（authority.score）
#   3. id 较小优先（保留早入库的）

# 法规编号正则（兜底，仅用于从 title 抽编号当 fallback key）
_REG_NUM_FALLBACK_RE = re.compile(
    r"(?:"
    r"\(EU\)\s*\d{4}/\d{3,5}"
    r"|3\d{4}[RLDC]\d{4}"      # CELEX
    r"|\bGB[/\s\-]?T?[\s\-]?\d{4,6}"
    r"|\d{1,3}\s*CFR\s*(?:Part\s*)?\d+"
    r"|\bUN\s*R\d+"
    r"|\b(?:EN|IEC|UL|ISO|JIS)\s*\d{3,5}"
    r"|\bCRA\b|Cyber\s+Resilience\s+Act"
    r"|\bAI\s+Act\b|\bPSTI\b|\bRoHS\b|\bREACH\b"
    r")",
    re.I,
)

_CONSOLIDATION_GROUP_LIMIT = 8   # 单次 LLM 调用最多比较的条目数


def _merge_markets(markets_list: list[str]) -> str:
    seen: list[str] = []
    for m in markets_list:
        for part in (p.strip() for p in (m or "").split("、") if p.strip()):
            if part not in seen:
                seen.append(part)
    return "、".join(seen)


def _row_signature(r) -> str | None:
    """计算一行的归一化 reg 键：优先用 rs.reg_id 字段，兜底从 title 抽。"""
    raw_reg_id = ""
    try:
        raw_reg_id = (r["reg_id"] or "").strip()
    except (IndexError, KeyError):
        pass
    if raw_reg_id:
        key = normalize_reg_id(raw_reg_id)
        if key:
            return key

    # 兜底：从 title 抽编号
    title = r["title"] or ""
    m = _REG_NUM_FALLBACK_RE.search(title)
    if m:
        candidate = m.group(0)
        return normalize_reg_id(candidate)
    return None


def _keeper_sort_key(r) -> tuple:
    """合并组内排序：非合成 > 权威分高 > id 小。"""
    is_synth = (r["full_text"] or "").startswith("[Gemini synthesis]")
    auth     = authority.score(r["source_url"] or "")
    return (1 if is_synth else 0, -auth, r["id"])


_CONSOLIDATION_SYSTEM      = prompts.load("consolidation_system").format(
    business_scope=_BUSINESS_SCOPE,
)
_CONSOLIDATION_PROMPT_TMPL = prompts.load("consolidation")


def _dedup_by_reg_signature(rows: list) -> tuple[int, int]:
    """
    Pass 1：按 normalize_reg_id 跨域确定性合并。
    同一法规的不同子条款 / 不同语言版本 / 不同实施细则切片 → 合并。
    """
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        key = _row_signature(r)
        if key:
            groups[key].append(r)

    merged_groups = deleted_entries = 0

    for key, group in groups.items():
        if len(group) < 2:
            continue

        group.sort(key=_keeper_sort_key)
        keep    = group[0]
        to_del  = group[1:]
        keep_id = keep["id"]

        merged_market = _merge_markets([r["affected_markets"] or "" for r in group])
        del_ids = [r["id"] for r in to_del]

        with get_connection() as conn:
            conn.execute(
                "UPDATE compliance_analysis SET affected_markets=? WHERE id=?",
                (merged_market, keep_id),
            )
            for did in del_ids:
                conn.execute("DELETE FROM compliance_analysis WHERE id=?", (did,))
                deleted_entries += 1

        merged_groups += 1
        titles = " / ".join((r["title"] or "")[:35] for r in group)
        _log.info("REGID-DEDUP key=%s keep=%d deleted=%s", key, keep_id, del_ids)
        print(f"  ✓ reg_id去重 [{key}]：保留 ID={keep_id}，删除 {del_ids}  [{titles[:80]}]")

    return merged_groups, deleted_entries


def _llm_consolidate_group(group: list, topic_hint: str, all_ids: set[int]) -> tuple[int, int]:
    """对同主题候选组（≤_CONSOLIDATION_GROUP_LIMIT 条）调用一次 LLM 判断。

    topic_hint：分组依据（如 "电助力自行车 / 🔴"），仅作 prompt 上下文，
    不限制 LLM 必须按此分组合并 —— LLM 仍可判断这是 N 个独立法规。
    """
    entries_text = ""
    for r in group:
        is_synth = (r["full_text"] or "").startswith("[Gemini synthesis]")
        tag      = "⚠️合成" if is_synth else "原文"
        req      = (r["compliance_requirement"] or "").replace("\n", " ")[:80]
        url_hint = (r["source_url"] or "")[:70]
        reg_hint = ""
        try:
            reg_hint = f" | reg_id:{r['reg_id']}" if r["reg_id"] else ""
        except (IndexError, KeyError):
            pass
        # 把 business_dimensions 显式列出——consolidation 的"维度独立"原则需要这个信号
        dim_str = ""
        try:
            dim_arr = json.loads(r["business_dimensions"] or "[]")
            if isinstance(dim_arr, list) and dim_arr:
                dim_str = f" | dims:{'/'.join(dim_arr)}"
        except Exception:
            pass
        entries_text += (
            f"[{r['id']}] ({tag}) {(r['title'] or '')[:60]}"
            f" | {r['affected_markets'] or '未知'} | {r['impact_level'] or '?'}"
            f"{reg_hint}"
            f"{dim_str}"
            f" | URL:{url_hint}"
            f" | {req}\n"
        )

    prompt = _CONSOLIDATION_PROMPT_TMPL.format(
        n=len(group), topic_hint=topic_hint, entries=entries_text,
    )
    try:
        resp_text = ai_client.call_json(
            prompt, system=_CONSOLIDATION_SYSTEM,
        )
    except Exception as e:
        _log.warning("Consolidation call failed for %s: %s", topic_hint, e)
        return 0, 0

    groups = parse_json_array(resp_text) or []
    merged = deleted = 0
    used: set[int] = set()

    for g in groups:
        if not isinstance(g, dict):
            continue
        gids   = g.get("group_ids") or []
        keep_id = g.get("keep_id")
        markets = (g.get("merged_markets") or "").strip()
        reason  = g.get("reason", "")

        if (
            not isinstance(gids, list) or len(gids) < 2
            or keep_id not in gids
            or not all(gid in all_ids for gid in gids)
            or any(gid in used for gid in gids)
        ):
            continue

        used.update(gids)
        delete_ids = [gid for gid in gids if gid != keep_id]

        with get_connection() as conn:
            if markets:
                conn.execute(
                    "UPDATE compliance_analysis SET affected_markets=? WHERE id=?",
                    (markets, keep_id),
                )
            for did in delete_ids:
                conn.execute("DELETE FROM compliance_analysis WHERE id=?", (did,))
                deleted += 1
        merged += 1
        _log.info("MERGE group=%s keep=%d reason=%s", gids, keep_id, reason)
        print(f"  ✓ 合并：{reason[:60]}  （保留 ID={keep_id}，删除 {delete_ids}）")

    return merged, deleted


_CONSOLIDATION_QUERY = """
    SELECT ca.id, rs.title, rs.source_url, rs.reg_id, ca.affected_markets,
           ca.impact_level, ca.affected_products, ca.business_dimensions,
           sc.full_text, ca.compliance_requirement
    FROM compliance_analysis ca
    JOIN scraped_content sc ON sc.id = ca.scraped_id
    JOIN raw_search_results rs ON rs.id = sc.raw_id
    WHERE rs.consolidated_into IS NULL
    ORDER BY ca.id
"""


def _run_consolidation() -> tuple[int, int]:
    """
    Stage 3 两 Pass：
      Pass 1：按 normalize_reg_id 跨域确定性合并（不调 LLM）
      Pass 2：剩余条目按 (affected_products, impact_level) 分组，调 LLM 积极合并
    """
    with get_connection() as conn:
        rows = conn.execute(_CONSOLIDATION_QUERY).fetchall()
    if len(rows) <= 1:
        return 0, 0

    # ── Pass 1：reg_id 跨域 ────────────────────────────────────────────────
    pre_m, pre_d = _dedup_by_reg_signature(rows)

    with get_connection() as conn:
        rows = conn.execute(_CONSOLIDATION_QUERY).fetchall()
    if len(rows) <= 1:
        return pre_m, pre_d

    # ── Pass 2：维度同质度 LLM 合并 ────────────────────────────────────────
    # 按 (sorted business_dimensions, affected_products) 分组——
    # consolidation prompt 已强调"维度不同不合并"，分组键先做这一层过滤，
    # 让 LLM 看到的候选同质度高，调用 ROI 提升。
    # 同时过滤 affected_products='不相关'：这些行已被 reporter 排除，
    # 不应再消耗 LLM 调用。
    topic_groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if (r["affected_products"] or "") == "不相关":
            continue
        try:
            dim_arr = json.loads(r["business_dimensions"] or "[]")
            if not isinstance(dim_arr, list):
                dim_arr = []
        except Exception:
            dim_arr = []
        dims_key = tuple(sorted(d for d in dim_arr if isinstance(d, str)))
        key = (dims_key or ("无维度",), r["affected_products"] or "未知")
        topic_groups[key].append(r)

    all_ids       = {r["id"] for r in rows}
    merged_total  = pre_m
    deleted_total = pre_d

    # 收集所有 LLM 任务（每个 batch 一次调用），并发执行
    # 注：不同 (维度集, 产品) 组的 ca.id 互斥，分批 slice 也互斥 → 无 DELETE 冲突
    tasks: list[tuple[list, str]] = []
    for key, grp in topic_groups.items():
        if len(grp) < 2:
            continue
        dims_label = "/".join(key[0]) if key[0] != ("无维度",) else "无维度"
        topic_hint = f"维度={dims_label} / 产品={key[1]}"
        for i in range(0, len(grp), _CONSOLIDATION_GROUP_LIMIT):
            batch = grp[i : i + _CONSOLIDATION_GROUP_LIMIT]
            if len(batch) < 2:
                continue
            tasks.append((batch, topic_hint))

    if not tasks:
        return merged_total, deleted_total

    print(f"  Stage 3 LLM consolidation：{len(tasks)} 组（并发 {_CONSOLIDATE_WORKERS}）")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_CONSOLIDATE_WORKERS, thread_name_prefix="consolidate",
    ) as ex:
        results = list(ex.map(
            lambda t: _llm_consolidate_group(t[0], t[1], all_ids),
            tasks,
        ))

    for m, d_ in results:
        merged_total  += m
        deleted_total += d_

    return merged_total, deleted_total


# ── 补填字段 ──────────────────────────────────────────────────────────────────

def _backfill_computed_fields() -> None:
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


if __name__ == "__main__":
    import sys
    run_analysis(skip_fallback="--skip-fallback" in sys.argv)
