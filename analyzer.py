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

import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, PRODUCT_LINES
from database import (
    get_connection,
    get_unanalyzed_content,
    get_raw_result,
    mark_analyzed,
    delete_orphan_scraped,
)
from utils import get_logger, parse_json_array, parse_json_object, reg_hash
from classify import (
    VALID_PRODUCTS,
    normalize_products,
    compute_products_display,
    compute_market_tier,
    institution,
)

_log = get_logger("analyzer")

ANALYSIS_MODEL = "gemini-flash-latest"
_MAX_TEXT      = 50_000

_VALID_IMPORTANCE = {"🔴", "🟡", "🟢"}
_PRODUCT_LIST     = "、".join(PRODUCT_LINES)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ── 系统提示（紧凑版）────────────────────────────────────────────────────────

_SYSTEM = f"""\
你是轻型电动出行设备（LEV）及关键零部件的合规分析师。

我司产品（标准名，affected_products 仅可填以下整机）：
  整机：{_PRODUCT_LIST}
  零部件：锂电池组、电机驱动系统、控制器、充电器、BMS 电池管理系统

任务：基于给定的法规原文，输出结构化合规分析。仅基于原文，不得添加未明确提及的信息。

────────────────────────────────────────
affected_products 填写规则（顿号分隔，只填整机标准名）
────────────────────────────────────────
• 法规仅针对零部件 → 判断哪些整机受影响，填整机名
  - 任何锂电池/充电器/BMS（无功率/容量/场景限制）→ 全部 5 类整机
  - 有特定功率段/应用场景 → 只选满足参数的整机
• e-bike / pedelec / EPAC / L1e-A → 电助力自行车
• L1e-B / L3e / moped / electric motorcycle → 电动摩托车
• micro-mobility / e-scooter / kick scooter / hoverboard / 개인형 이동장치 / 電動キックボード → 电动滑板车、电动平衡车
• 共享出行 / fleet / dockless → 电动滑板车
• robotic mower / lawn mower / tondeuse / 草刈機 → 智能割草机
• 通用电气安全 / EMC / RoHS / REACH 覆盖全部电动设备 → 全部 5 类整机

「不相关」仅限：四轮汽车、船舶、航空器、固定装置、建材等与本公司全部产品无任何交集的领域。
凡涉及电池、充电、电动车辆、个人移动设备、产品安全或 EMC，一律选相关整机。

────────────────────────────────────────
affected_markets 填写规范（顿号分隔，选最准确层级）
────────────────────────────────────────
全球通用                — 法规明确适用全球
欧盟                    — EU 整体法规
[欧洲国名]              — 单一成员国（德国/法国/英国/意大利/荷兰…）
北美                    — 美加共同法规
美国（联邦）            — 仅适用美联邦
美国（[州名]）          — 仅适用某州，如：美国（加州）
加拿大（联邦）/（[省]） — 仅适用加拿大联邦/某省
澳新 / 澳大利亚（联邦） / 新西兰
日本 / 韩国 / 俄罗斯

────────────────────────────────────────
重要度评分（importance / importance_note）
────────────────────────────────────────

第一步「法规生命周期阶段」：
  阶段 4：已颁布且过渡期 ≤ 12 个月，或执法已启动
  阶段 3：已颁布，过渡期 12–36 个月
  阶段 2：正式草案已进入立法程序，有公开时间表
  阶段 1：咨询文件/绿皮书/政策讨论，无正式草案

第二步「合规责任性质」：
  C3：我方（生产者/进口商）为直接义务主体，合规为进入市场销售的前提（不合规即停售）
  C2：我方为直接义务主体，但合规为持续经营条件（在售期间可同步完成）
  C1：我方非直接义务主体（义务落于用户/零售商/其他方），或我方适用性待确认

第三步矩阵：
              C3      C2      C1
  阶段 4   →  🔴      🔴      🟡
  阶段 3   →  🔴      🟡      🟢
  阶段 2   →  🟡      🟡      🟢
  阶段 1   →  🟡      🟢      🟢

importance 字段：仅输出符号 🔴/🟡/🟢
importance_note 字段统一格式（必须以「阶段N + CX → 档位」开头）：
  基础形式：              「阶段3 + C3 → 🔴」
  跨市场或多产品类别：    「阶段3 + C3 → 🔴 ｜ 跨市场/跨品类」
  C1 且执法在 12 个月内：  「阶段4 + C1 → 🟡 ｜ 用户端执法在即」
  适用性待确认：          「阶段3 + C3 → 🟡 ｜ 适用性待确认」
  信息严重不足（无法判断阶段或责任性质）：
                          「阶段?（信息不足）+ C? → 🟡 ｜ 合理推断」

参考案例（评分逻辑示意，不依赖具体时间）：
  • 已颁布 + 12 个月内强制全面实施 + 我方为直接义务主体 → 阶段 4 + C3 → 🔴
  • 已颁布 + 过渡期 24 个月 + 我方为直接义务主体        → 阶段 3 + C3 → 🔴
  • 草案咨询中 + 我方为直接义务主体                     → 阶段 2 + C3 → 🟡
  • 已颁布 + 强制日临近 + 义务落于消费者/零售商         → 阶段 4 + C1 → 🟡 ｜ 用户端执法在即
  • 政策讨论 + 我方为附加合规条件                       → 阶段 1 + C2 → 🟢
"""

# ── 主分析提示模板 ────────────────────────────────────────────────────────────

_PROMPT_TMPL = """\
今日日期：{today}

法规名称：{title}
官方链接：{url}
适用市场：{market}
相关性说明：{relevance}

以下是从官方网站抓取的原文内容：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{scraped_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

若原文与标题不符或与我司产品无关，affected_products 填「不相关」。
评分规则、affected_products / affected_markets / importance_note 的格式均见系统提示。

输出严格 JSON（单个对象，不含 markdown）：
{{
  "requirement":     "中文 3–5 条要点（每条以数字编号，如：1. ... 2. ...）",
  "dates": {{
    "publish":            "YYYY-MM-DD 或 null",
    "effective":          "YYYY-MM-DD 或 null",
    "enforcements": [
      {{"date": "YYYY-MM-DD", "scope": "适用条款/义务范围简述"}}
    ],
    "consultation_close": "YYYY-MM-DD 或 null"
  }},
  "deadline":        "最紧迫强制截止日 YYYY-MM-DD 或 null",
  "importance":      "🔴 或 🟡 或 🟢（仅符号，三选一）",
  "importance_note": "「阶段N + CX → 档位」开头，按系统提示格式",
  "worst_case":      "不合规最坏后果（罚款金额/禁售/召回/吊销等）",
  "business_impact": "对我司影响最大的 3 个要点（整车出口/供应链/终端销售/认证成本/产品设计变更等维度）",
  "affected_products": "从以下选项选出（顿号分隔）：{product_list}；均不适用填不相关",
  "affected_markets":  "受影响市场（按系统提示规范）"
}}"""

# ── 降级合成专用提示 ──────────────────────────────────────────────────────────

_FALLBACK_SYSTEM = _SYSTEM + """\

────────────────────────────────────────
⚠️ 当前为「降级合成」模式 — 以下规则覆盖（override）上述参考案例中的档位选择：

下文是 Gemini 通过 Google Search 合成的资料（非官方原文，含幻觉风险）。
请按以下额外约束：
  • importance 一律不超过 🟡（即使评分逻辑指向 🔴 — 合成数据置信度天然低于原文，
    上述参考案例中的 🔴 档位在本模式下统一降为 🟡）
  • 不要凭空写出具体日期或罚款金额；不确定时留 null 或在 worst_case 写「待确认」
  • 其他字段（requirement / business_impact / affected_products / affected_markets）
    仍按主分析规则填写，但只引用合成资料明确提及的信息
注：「数据来源：AI 合成」标记由系统在事后自动追加，无需你写。
"""

_FALLBACK_PROMPT_TMPL = """\
今日日期：{today}

法规名称：{title}
官方链接：{url}
适用市场：{market}
相关性说明：{relevance}

⚠️ 以下内容由 Gemini 通过 Google Search 合成（非官方原文，置信度低）：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{scraped_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

请按系统提示中的「降级合成」额外约束作答：importance 不超过 🟡，
不要凭空补充资料未提及的具体日期、罚款金额。

输出严格 JSON（单个对象，不含 markdown）：
{{
  "requirement":     "中文 3–5 条要点（每条以数字编号）",
  "dates": {{
    "publish":            "YYYY-MM-DD 或 null",
    "effective":          "YYYY-MM-DD 或 null",
    "enforcements": [{{"date": "YYYY-MM-DD", "scope": "..."}}],
    "consultation_close": "YYYY-MM-DD 或 null"
  }},
  "deadline":        "YYYY-MM-DD 或 null",
  "importance":      "🟡 或 🟢（合成模式下绝不输出 🔴）",
  "importance_note": "「阶段N + CX → 档位」格式，例如：阶段3 + C3 → 🟡",
  "worst_case":      "若合成资料未提及则写「待确认」",
  "business_impact": "保守评估对我司影响要点（仅引用合成资料明确提及的信息）",
  "affected_products": "从以下选项选出：{product_list}；均不适用填不相关",
  "affected_markets":  "受影响市场（按系统提示规范）"
}}"""

_SYNTHESIS_WARNING = "⚠️ 原文抓取失败，此条目基于 AI 合成，请人工核实后再使用。"


# ── Gemini 调用 ───────────────────────────────────────────────────────────────

def _call_gemini(prompt: str, *, system: str = _SYSTEM) -> str:
    """无 grounding 调用，并请求 JSON 响应。"""
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
    )
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            resp = _get_client().models.generate_content(
                model=ANALYSIS_MODEL,
                contents=prompt,
                config=cfg,
            )
            return resp.text or ""
        except Exception as e:
            last_err = e
            if attempt < 2:
                err = str(e).lower()
                rate = "429" in err or "quota" in err or "rate" in err
                time.sleep(60 if rate else 8 * (attempt + 1))
    raise RuntimeError(f"Gemini call failed: {last_err}")


def _gemini_grounding_fetch(title: str, url: str, market: str, relevance: str) -> str | None:
    """grounded fetch：用 Google Search 搜法规内容。仅用于降级合成路径。"""
    fetch_system = (
        "你是法规内容检索助手。请尽可能引用官方原文（条款、范围、强制日、罚则），"
        "不添加分析或评论。"
    )
    prompt = (
        f"请搜索以下法规的官方内容，尽可能引用原文：\n"
        f"法规名称：{title}\n"
        f"官方链接：{url}\n"
        f"适用市场：{market}\n"
        f"背景说明：{relevance}\n\n"
        "只返回法规内容，不要加分析评论。"
    )
    try:
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=fetch_system,
        )
        resp = _get_client().models.generate_content(
            model=ANALYSIS_MODEL, contents=prompt, config=cfg,
        )
        text = (resp.text or "").strip()
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

    dates_raw    = result.get("dates") or {}
    enforcements = dates_raw.get("enforcements") or []
    key_dates    = json.dumps({
        "publish":            dates_raw.get("publish"),
        "effective":          dates_raw.get("effective"),
        "enforcements":       enforcements if isinstance(enforcements, list) else [],
        "consultation_close": dates_raw.get("consultation_close"),
    }, ensure_ascii=False)

    worst_case      = (result.get("worst_case") or "").strip()
    importance_note = (result.get("importance_note") or "").strip()
    biz             = (result.get("business_impact") or "").strip()
    if importance_note:
        biz = f"{biz}\n\n（重要度标注：{importance_note}）".strip()
    if extra_biz:
        biz = f"{extra_biz}\n{biz}".strip()

    affected_markets_str   = (result.get("affected_markets") or market or "").strip()
    products_display       = compute_products_display(products)
    market_tier_val        = compute_market_tier(affected_markets_str)
    source_inst, source_lg = institution(url, market)

    return {
        "importance":        importance,
        "products":          products,
        "key_dates":         key_dates,
        "worst_case":        worst_case,
        "biz":               biz,
        "affected_markets":  affected_markets_str,
        "products_display":  products_display,
        "market_tier":       market_tier_val,
        "source_inst":       source_inst,
        "source_lg":         source_lg,
        "requirement":       (result.get("requirement") or "").strip(),
        "deadline":          result.get("deadline"),
        "url":               url,
    }


def _insert_analysis_row(conn, scraped_id: int, v: dict, h: str) -> None:
    src_url = v.get("url") or ""
    sources = json.dumps([{"url": src_url}] if src_url else [], ensure_ascii=False)
    conn.execute("""
        INSERT OR IGNORE INTO compliance_analysis
            (scraped_id, compliance_requirement, compliance_deadline,
             key_dates, action_items, impact_level, affected_products,
             affected_markets, worst_case_scenario, business_impact,
             sources, content_hash, analysis_date,
             affected_products_display, market_tier,
             source_institution, source_language)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        sources,
        h,
        datetime.now().isoformat(),
        v["products_display"],
        v["market_tier"],
        v["source_inst"],
        v["source_lg"],
    ))


# ── 主入口 ────────────────────────────────────────────────────────────────────

def run_analysis() -> tuple[int, int, int]:
    """
    返回 (analyzed, skipped_dup, failed)。
    """
    unanalyzed = get_unanalyzed_content()
    if not unanalyzed:
        print("  没有待分析的内容。")
        _backfill_computed_fields()
        _requeue_navigation_failures()
        fa, ff = _run_fallback()
        mc, dc = _run_consolidation()
        if mc > 0:
            print(f"\n  整合完成：合并 {mc} 组，删除 {dc} 条重复记录")
        return fa, 0, ff

    total    = len(unanalyzed)
    analyzed = skipped = failed = 0

    print(f"\n  共 {total} 条待分析\n")

    for i, sc_row in enumerate(unanalyzed, 1):
        raw       = get_raw_result(sc_row["raw_id"])
        title     = (raw["title"]      if raw else "") or ""
        url       = (raw["source_url"] if raw else "") or ""
        market    = (raw["market"]     if raw else "") or ""
        relevance = (raw["snippet"]    if raw else "") or ""
        full_text = (sc_row["full_text"] or "")[:_MAX_TEXT]

        print(f"  [{i:>3}/{total}] {title[:52]}", end=" ... ", flush=True)

        if not full_text.strip():
            mark_analyzed(sc_row["id"])
            failed += 1
            print("✗ 内容为空，跳过")
            continue

        h      = reg_hash(title)
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        with get_connection() as conn:
            if conn.execute(
                "SELECT 1 FROM compliance_analysis WHERE content_hash=? AND analysis_date>=?",
                (h, cutoff),
            ).fetchone():
                mark_analyzed(sc_row["id"])
                skipped += 1
                print("→ 重复，跳过")
                continue

        # 上一轮 fallback 留下的合成内容若被重置 ai_analyzed=0 → 走降级 prompt
        is_synth = full_text.startswith("[Gemini synthesis]")
        if is_synth:
            tmpl       = _FALLBACK_PROMPT_TMPL
            sys_prompt = _FALLBACK_SYSTEM
            extra_biz  = _SYNTHESIS_WARNING
        else:
            tmpl       = _PROMPT_TMPL
            sys_prompt = _SYSTEM
            extra_biz  = None

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
                failed += 1
                _log.error("JSON parse fail scraped_id=%d", sc_row["id"])
                print("✗ JSON 解析失败")
                continue

            if is_synth:
                _enforce_fallback_caps(result)

            values = _build_analysis_values(result, title, url, market, extra_biz=extra_biz)
            with get_connection() as conn:
                _insert_analysis_row(conn, sc_row["id"], values, h)

            mark_analyzed(sc_row["id"])
            analyzed += 1
            importance = values["importance"]
            products   = values["products"]
            _log.info("OK scraped_id=%d importance=%s products=%s",
                      sc_row["id"], importance, products)
            tag = "[不相关]" if products == "不相关" else products[:30]
            print(f"→ {importance}  {tag}")

        except Exception as e:
            mark_analyzed(sc_row["id"])
            failed += 1
            _log.error("FAIL scraped_id=%d: %s", sc_row["id"], e)
            print(f"✗ {e}")

        if i < total:
            time.sleep(1)

    print(f"\n  分析完成：成功 {analyzed}，重复跳过 {skipped}，失败 {failed}")

    _backfill_computed_fields()
    _requeue_navigation_failures()
    fa, ff = _run_fallback()

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


# ── 导航页回收（直接抓到却被判'不相关'且字数极少 → 转入降级合成）──────────────

_NAV_PAGE_WORD_THRESHOLD = 200


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
              AND (sc.word_count IS NULL OR sc.word_count < ?)
        """, (_NAV_PAGE_WORD_THRESHOLD,)).fetchall()

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

    total    = len(failed_rows)
    analyzed = failed = 0

    print(f"\n  ── 降级处理（Gemini 合成）：{total} 条 ──\n")

    for i, row in enumerate(failed_rows, 1):
        title     = (row["title"]      or "").strip()
        url       = (row["source_url"] or "").strip()
        market    = (row["market"]     or "").strip()
        relevance = (row["snippet"]    or "").strip()
        h         = reg_hash(title)

        print(f"  [{i:>3}/{total}] {title[:52]}", end=" ... ", flush=True)

        synth_text = _gemini_grounding_fetch(title, url, market, relevance)
        if not synth_text:
            _mark_manual(row["id"])
            failed += 1
            print("✗ 合成失败 → 需人工")
            continue

        prompt = _FALLBACK_PROMPT_TMPL.format(
            today=datetime.now().strftime("%Y-%m-%d"),
            title=title or "（未知）",
            url=url or "（未知）",
            market=market or "（未知）",
            relevance=relevance or "（无说明）",
            scraped_text=synth_text[:_MAX_TEXT],
            product_list=_PRODUCT_LIST,
        )

        try:
            text   = _call_gemini(prompt, system=_FALLBACK_SYSTEM)
            result = parse_json_object(text)
            if not result:
                _mark_manual(row["id"])
                failed += 1
                print("✗ JSON 解析失败 → 需人工")
                continue

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

            analyzed += 1
            importance = values["importance"]
            products   = values["products"]
            _log.info("SYNTH raw_id=%d importance=%s products=%s",
                      row["id"], importance, products)
            tag = "[不相关]" if products == "不相关" else products[:28]
            print(f"→ {importance}  {tag}  ⚠️")

        except Exception as e:
            _mark_manual(row["id"])
            failed += 1
            _log.error("SYNTH FAIL raw_id=%d: %s", row["id"], e)
            print(f"✗ {e} → 需人工")

        if i < total:
            time.sleep(1)

    print(f"\n  降级完成：合成分析 {analyzed}，失败 {failed}（均已标记需人工复核）")
    return analyzed, failed


def _mark_manual(raw_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_search_results SET scrape_status='需人工' WHERE id=?",
            (raw_id,),
        )


# ── 整合去重 ──────────────────────────────────────────────────────────────────

_REG_NUM_RE = re.compile(
    r"(?:"
    r"\d{4}/\d{3,4}"           # EU Reg：2023/1542
    r"|CFR\s*Part\s*\d+"       # 美 CFR
    r"|\bPart\s+\d+"
    r"|\bAIS[-\s]*\d+"         # 印度 AIS
    r"|\bEN\s+\d{4,5}"
    r"|\bIEC\s+\d{4,5}"
    r"|\bUL\s+\d{4}"
    r")",
    re.I,
)

_CONSOLIDATION_GROUP_LIMIT = 8   # 单次 LLM 调用最多比较的条目数


def _extract_reg_ids(title: str) -> set[str]:
    return {m.group(0).upper().replace(" ", "") for m in _REG_NUM_RE.finditer(title or "")}


def _merge_markets(markets_list: list[str]) -> str:
    seen: list[str] = []
    for m in markets_list:
        for part in (p.strip() for p in (m or "").split("、") if p.strip()):
            if part not in seen:
                seen.append(part)
    return "、".join(seen)


def _domain_of(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"https?://([^/]+)", url)
    return m.group(1).lower() if m else ""


_CONSOLIDATION_SYSTEM = """\
你是合规数据库管理员，负责识别代表同一监管要求的重复条目。
判断标准必须极其保守：只有在几乎 100% 确定时才建议合并。
"""

_CONSOLIDATION_PROMPT_TMPL = """\
以下 {n} 个条目来自同一域名（{domain}）。
注：完全相同 URL + 同一编号的条目已被前置确定性合并；
你看到的应是「同域名但 URL 不同」或「URL 不同但疑似同一监管要求」的情形。

格式：[ID] (来源标记) 标题 | 市场 | 影响等级 | URL | 要点摘要

{entries}

任务：找出实质代表同一监管要求、应当合并的条目组。

合并标准（保守，仅高度确定时合并）：
1. 欧盟法规 + UK retained EU law，且要求实质一致
2. 同一国际标准的不同地区实施版本（IEC → EN/AS/GB），且条文一致
3. 同一法规的不同语言版本（如官方公报 EN + DE 版本同号）
4. 标题含相同法规编号/Part 号/Regulation 编号且要点摘要互相覆盖

绝对不合并：
- 主题相似但实为独立立法（不同编号/名称）
- 欧盟法规 vs 各成员国实施细则（即使要求来源相同）
- 同框架下不同子法规（如 EU Battery Reg 主体 vs 委托法规）
- 要求存在实质差异
- 仅共享同一域名根域而无具体编号关联

输出严格 JSON 数组（无需合并 → []，不含说明文字）：
[
  {{
    "group_ids": [整数 ID 列表，至少 2 个],
    "keep_id": 保留哪条（整数）,
    "merged_markets": "合并后的市场字段（顿号分隔，按系统提示规范）",
    "reason": "合并理由（一句话，必须指出共同的法规编号或来源）"
  }}
]"""


def _dedup_by_url(rows: list) -> tuple[int, int]:
    """确定性合并：来源 URL 完全相同 + 标题含同一法规编号。"""
    url_groups: dict[str, list] = defaultdict(list)
    for r in rows:
        url = (r["source_url"] or "").strip()
        if url:
            url_groups[url].append(r)

    merged_groups = deleted_entries = 0
    used_ids: set[int] = set()

    for url, group in url_groups.items():
        if len(group) < 2:
            continue

        subgroups: list[list] = []
        for entry in group:
            if entry["id"] in used_ids:
                continue
            ids_e = _extract_reg_ids(entry["title"])
            placed = False
            for sg in subgroups:
                if ids_e & _extract_reg_ids(sg[0]["title"]):
                    sg.append(entry)
                    placed = True
                    break
            if not placed:
                subgroups.append([entry])

        for sg in subgroups:
            if len(sg) < 2:
                continue
            sg.sort(key=lambda r: (
                1 if (r["full_text"] or "").startswith("[Gemini synthesis]") else 0,
                r["id"],
            ))
            keep    = sg[0]
            to_del  = sg[1:]
            keep_id = keep["id"]
            merged_market = _merge_markets([r["affected_markets"] or "" for r in sg])
            del_ids = [r["id"] for r in to_del]

            with get_connection() as conn:
                conn.execute(
                    "UPDATE compliance_analysis SET affected_markets=? WHERE id=?",
                    (merged_market, keep_id),
                )
                for did in del_ids:
                    conn.execute("DELETE FROM compliance_analysis WHERE id=?", (did,))
                    deleted_entries += 1

            used_ids.update(r["id"] for r in sg)
            merged_groups += 1
            titles = " / ".join((r["title"] or "")[:40] for r in sg)
            _log.info("URL-DEDUP keep=%d deleted=%s url=%s",
                      keep_id, del_ids, url[:60])
            print(f"  ✓ URL去重：保留 ID={keep_id}，删除 {del_ids}  [{titles[:70]}]")

    return merged_groups, deleted_entries


def _llm_consolidate_group(group: list, domain: str, all_ids: set[int]) -> tuple[int, int]:
    """对同域候选组（≤_CONSOLIDATION_GROUP_LIMIT 条）调用一次 LLM 判断。"""
    entries_text = ""
    for r in group:
        is_synth = (r["full_text"] or "").startswith("[Gemini synthesis]")
        tag      = "⚠️合成" if is_synth else "原文"
        req      = (r["compliance_requirement"] or "").replace("\n", " ")[:80]
        url_hint = (r["source_url"] or "")[:70]
        entries_text += (
            f"[{r['id']}] ({tag}) {(r['title'] or '')[:60]}"
            f" | {r['affected_markets'] or '未知'} | {r['impact_level'] or '?'}"
            f" | URL:{url_hint}"
            f" | {req}\n"
        )

    prompt = _CONSOLIDATION_PROMPT_TMPL.format(
        n=len(group), domain=domain, entries=entries_text,
    )
    cfg = types.GenerateContentConfig(
        system_instruction=_CONSOLIDATION_SYSTEM,
        response_mime_type="application/json",
    )
    try:
        resp_text = _get_client().models.generate_content(
            model=ANALYSIS_MODEL, contents=prompt, config=cfg,
        ).text or ""
    except Exception as e:
        _log.warning("Consolidation call failed for %s: %s", domain, e)
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


def _run_consolidation() -> tuple[int, int]:
    """两阶段：① 同 URL+同法规编号确定性合并 ② 同域候选交给 LLM 二次判断。"""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ca.id, rs.title, rs.source_url, ca.affected_markets, ca.impact_level,
                   sc.full_text, ca.compliance_requirement
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            ORDER BY ca.id
        """).fetchall()

    if len(rows) <= 1:
        return 0, 0

    pre_m, pre_d = _dedup_by_url(rows)

    # 第二阶段前重新拉一次（已被前置删除的不参与）
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ca.id, rs.title, rs.source_url, ca.affected_markets, ca.impact_level,
                   sc.full_text, ca.compliance_requirement
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            ORDER BY ca.id
        """).fetchall()
    if len(rows) <= 1:
        return pre_m, pre_d

    # 按域名分组；同域 ≥2 条且总条目 ≤ 限制时调用 LLM
    domain_groups: dict[str, list] = defaultdict(list)
    for r in rows:
        d = _domain_of(r["source_url"] or "")
        if d:
            domain_groups[d].append(r)

    all_ids = {r["id"] for r in rows}
    merged_total = pre_m
    deleted_total = pre_d

    for domain, grp in domain_groups.items():
        if len(grp) < 2:
            continue
        # 对超大组分批，每批最多 _CONSOLIDATION_GROUP_LIMIT
        for i in range(0, len(grp), _CONSOLIDATION_GROUP_LIMIT):
            batch = grp[i : i + _CONSOLIDATION_GROUP_LIMIT]
            if len(batch) < 2:
                continue
            m, d_ = _llm_consolidate_group(batch, domain, all_ids)
            merged_total  += m
            deleted_total += d_
            time.sleep(1)

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
    run_analysis()
