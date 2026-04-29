"""
降级合成路径：抓取失败 / 导航页 → Gemini grounded fetch 合成内容 → 分析。

核心组件：
  • _should_fallback             启发式过滤：哪些失败条目值得跑合成
  • requeue_navigation_failures  把误判"不相关"的导航页转入合成队列
  • _gemini_grounding_fetch      用 Google Search grounded 调用合成法规内容
  • enforce_fallback_caps        合成路径不变量：importance 不超过 🟡 + 标注来源
  • _fallback_one / run_fallback 并发执行 + 写库

合成出的 scraped_content 用 "[Gemini synthesis]" 前缀标记，
analyzer.main._analyze_one 检测到该前缀会自动走降级 prompt。
"""
from __future__ import annotations

import concurrent.futures
import re
from datetime import datetime

import ai_client
import prompts
from database import get_connection
from utils import get_logger, parse_json_object, reg_hash

from ._shared import (
    FALLBACK_SYSTEM, FALLBACK_PROMPT_TMPL,
    SYNTHESIS_WARNING, PRODUCT_LIST,
    safe_print, truncate_smart,
)
from .values import build_analysis_values, insert_analysis_row

_log = get_logger("analyzer")

# 并发参数
_FALLBACK_WORKERS = 5   # 降级合成含 grounded 调用，更慢

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

# 导航页字符阈值：小于该值且被判"不相关"的，转入合成队列
# 用字符数而非 word_count：中文段落无空格，word_count 度量在 CJK 下不稳定。
# 800 字符约等于半页 A4 正文，覆盖中英文导航页的典型规模。
_NAV_PAGE_CHAR_THRESHOLD = 800


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


def enforce_fallback_caps(result: dict) -> None:
    """合成路径不变量：importance 不超过 🟡，importance_note 必含「数据来源：AI 合成」。"""
    if result.get("importance") == "🔴":
        result["importance"] = "🟡"
    note = (result.get("importance_note") or "").strip()
    if "数据来源：AI 合成" not in note:
        note = (note + " ｜ 数据来源：AI 合成").strip(" ｜")
    result["importance_note"] = note


def requeue_navigation_failures() -> int:
    """把空壳/导航页/极短抓取转入降级合成队列。

    早期版本依赖 AI 主分析自报 affected_products='不相关'——但 AI 看到
    知名法规标题（如 "Basel Convention BC-15/18"）时，会拿训练知识凭空
    "自信合成"，根本不自报"不相关"，从而绕过此关卡。

    现在改为不依赖 AI 自报：只要 full_text 长度低于导航页阈值就判定
    主分析不可信，强制重走 grounded 合成路径并打 ⚠️ AI 合成 标识。
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ca.id, rs.id AS raw_id, rs.title
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            WHERE (sc.full_text IS NULL OR sc.full_text NOT LIKE '[Gemini synthesis]%')
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


def _mark_manual(raw_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_search_results SET scrape_status='需人工' WHERE id=?",
            (raw_id,),
        )


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
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ 合成失败 → 需人工")
        return "fail"

    prompt = FALLBACK_PROMPT_TMPL.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        title=title or "（未知）",
        url=url or "（未知）",
        market=market or "（未知）",
        relevance=relevance or "（无说明）",
        scraped_text=truncate_smart(synth_text),
        product_list=PRODUCT_LIST,
    )

    try:
        text   = ai_client.call_json(prompt, system=FALLBACK_SYSTEM)
        result = parse_json_object(text)
        if not result:
            _mark_manual(row["id"])
            safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ JSON 解析失败 → 需人工")
            return "fail"

        enforce_fallback_caps(result)
        values = build_analysis_values(
            result, title, url, market, extra_biz=SYNTHESIS_WARNING,
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
            insert_analysis_row(conn, sc_id, values, h)
            conn.execute(
                "UPDATE raw_search_results SET scrape_status='需人工' WHERE id=?",
                (row["id"],),
            )

        importance = values["importance"]
        products   = values["products"]
        _log.info("SYNTH raw_id=%d importance=%s products=%s",
                  row["id"], importance, products)
        tag = "[不相关]" if products == "不相关" else products[:28]
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} → {importance}  {tag}  ⚠️")
        return "ok"

    except Exception as e:
        _mark_manual(row["id"])
        _log.error("SYNTH FAIL raw_id=%d: %s", row["id"], e)
        safe_print(f"  [{idx:>3}/{total}] {title[:52]} ✗ {e} → 需人工")
        return "fail"


def run_fallback() -> tuple[int, int]:
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
