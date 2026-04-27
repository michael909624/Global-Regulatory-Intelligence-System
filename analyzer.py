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

import ai_client
import prompts
from config import PRODUCT_LINES
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

_MAX_TEXT     = 50_000
_VALID_IMPORTANCE = {"🔴", "🟡", "🟢"}
_PRODUCT_LIST = "、".join(PRODUCT_LINES)


# ── 提示词(从 prompts/ 加载) ────────────────────────────────────────────────

_SYSTEM = prompts.load("analyzer_system").format(product_list=_PRODUCT_LIST)
_PROMPT_TMPL          = prompts.load("analyzer_main")
_FALLBACK_SYSTEM      = _SYSTEM + prompts.load("analyzer_fallback_extension")
_FALLBACK_PROMPT_TMPL = prompts.load("analyzer_fallback")

_SYNTHESIS_WARNING = "⚠️ 原文抓取失败，此条目基于 AI 合成，请人工核实后再使用。"


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


_CONSOLIDATION_SYSTEM      = prompts.load("consolidation_system")
_CONSOLIDATION_PROMPT_TMPL = prompts.load("consolidation")


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
    try:
        resp_text = ai_client.call_json(
            prompt, system=_CONSOLIDATION_SYSTEM,
        )
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
