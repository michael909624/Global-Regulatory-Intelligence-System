"""
Reporter：生成 Excel 周报。
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import authority
import rules
from config import REPORTS_DIR
from database import init_db, get_all_analyses, get_week_analyses, get_manual_followup

# 产品排序优先级:行号即 rank(1 = 最优先);外移到 rules/product_sort_order.txt
_PRODUCT_RANKS: dict[str, int] = {
    kw: i for i, kw in enumerate(rules.load_lines("product_sort_order"), 1)
}

# 与 analyzer/_shared.SYNTHESIS_WARNING 保持一致——避免循环依赖直接复制字符串
_SYNTHESIS_WARNING = "⚠️ 原文抓取失败，此条目基于 AI 合成，请人工核实后再使用。"

# Excel 单元格字符上限（openpyxl 保护性裁剪）
_EXCEL_CELL_MAX = 32_000


# ── XML 安全清洗 ──────────────────────────────────────────────────────────────

_ILLEGAL_XML_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff￾￿]"
)


def _clean(v):
    if not isinstance(v, str):
        return v
    s = _ILLEGAL_XML_RE.sub("", v)
    if len(s) > _EXCEL_CELL_MAX:
        s = s[:_EXCEL_CELL_MAX - 5] + "[...]"
    return s


# ── 列宽行高 ──────────────────────────────────────────────────────────────────

_MAIN_COL_CAPS   = [5, 28, 16, 50, 52, 52, 34, 52]
_MANUAL_COL_CAPS = [50, 70, 12]

# 从 business_impact 字段过滤掉 analyzer 历史追加的"（重要度标注：…）"后缀
_IMPORTANCE_NOTE_PAT = re.compile(r"[\n\s]*（重要度标注：[^）]*）", re.MULTILINE)


def _cjk_len(s: str) -> float:
    if not s:
        return 0
    total = 0
    for ch in s:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F or
                0xFF00 <= cp <= 0xFFEF or 0xAC00 <= cp <= 0xD7AF or
                0x3040 <= cp <= 0x30FF):
            total += 2
        else:
            total += 1
    return total


def _fit_sheet(ws, col_caps=None) -> None:
    """两遍扫描自适应：先定列宽，再按列宽估算行高。"""
    max_col = ws.max_column
    col_ws  = {c: 0.0 for c in range(1, max_col + 1)}

    for row in ws.iter_rows(min_row=1):
        for cell in row:
            if cell.value is None or cell.column > max_col:
                continue
            lines = str(cell.value).split("\n")
            w = max((_cjk_len(ln) for ln in lines), default=0)
            if w > col_ws[cell.column]:
                col_ws[cell.column] = w

    for c in range(1, max_col + 1):
        cap    = col_caps[c - 1] if col_caps and c - 1 < len(col_caps) else 60
        fitted = min(max(col_ws[c] + 2, 3), cap)
        ws.column_dimensions[get_column_letter(c)].width = fitted
        col_ws[c] = fitted

    for row in ws.iter_rows(min_row=2):
        row_idx   = row[0].row
        max_lines = 1
        for cell in row:
            if cell.value is None:
                continue
            col_w   = col_ws.get(cell.column, 20)
            n_lines = 0
            for ln in str(cell.value).split("\n"):
                vlen     = _cjk_len(ln)
                n_lines += max(1, math.ceil(vlen / col_w)) if col_w > 0 else 1
            max_lines = max(max_lines, n_lines)
        ws.row_dimensions[row_idx].height = max(max_lines * 15, 15)


# ── 数据格式化 ────────────────────────────────────────────────────────────────

def _fmt_dates(deadline: str | None, key_dates_json: str | None) -> str:
    kd: dict = {}
    if key_dates_json:
        try:
            kd = json.loads(key_dates_json)
        except Exception:
            pass

    parts: list[str] = []
    seen: set[str] = set()

    if kd.get("publish"):
        parts.append(f"发布日：{kd['publish']}")
        seen.add(kd["publish"])

    if kd.get("effective") and kd["effective"] not in seen:
        parts.append(f"生效日：{kd['effective']}")
        seen.add(kd["effective"])

    for entry in (kd.get("enforcements") or []):
        if not isinstance(entry, dict):
            continue
        d     = (entry.get("date")  or "").strip()
        scope = (entry.get("scope") or "").strip()
        if d and d not in seen:
            parts.append(f"强制日：{d}（{scope}）" if scope else f"强制日：{d}")
            seen.add(d)

    if kd.get("consultation_close") and kd["consultation_close"] not in seen:
        parts.append(f"咨询截止：{kd['consultation_close']}")

    # 极旧记录 fallback：key_dates 全空时仅显示 deadline
    if not parts and deadline:
        parts.append(f"截止日：{deadline}")

    return "\n".join(parts) or "待确认"


def _fmt_source(source_institution: str | None, source_language: str | None,
                title_orig: str | None, url: str | None) -> str:
    if source_institution:
        inst = source_institution
        lang = source_language or "—"
    elif url:
        try:
            host  = urlparse(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            parts = host.split(".")
            inst  = parts[-2].upper() if len(parts) >= 2 else host
        except Exception:
            inst = "—"
        lang = "—"
    else:
        inst, lang = "—", "—"

    parts = []
    if title_orig:
        parts.append(title_orig)
    parts.append(f"{inst}（{lang}）")
    if url:
        parts.append(url)
    return "\n".join(p for p in parts if p)


# ── 样式 ──────────────────────────────────────────────────────────────────────

# (badge_bg, badge_fg, row_light, row_dark)
_IMPACT_PALETTE = {
    "🔴": ("C0392B", "FFFFFF", "FDECEA", "FAD7D4"),
    "🟡": ("D4820A", "FFFFFF", "FEF9E7", "FDEAB7"),
    "🟢": ("1E8449", "FFFFFF", "EAFAF1", "D5F5E3"),
}
_BADGE_TEXT = {"🔴": "🔴", "🟡": "🟡", "🟢": "🟢"}

_HEADER_FILL = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_BODY_FONT   = Font(size=10)
_WRAP        = Alignment(wrap_text=True, vertical="center")
_CENTER      = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _border(top_heavy: bool = False) -> Border:
    thin  = Side(border_style="thin",   color="D0D0D0")
    heavy = Side(border_style="medium", color="999999")
    return Border(
        left=thin, right=thin,
        top=heavy if top_heavy else thin,
        bottom=thin,
    )


# ── Sheet 构造 ────────────────────────────────────────────────────────────────

HEADERS = ["重要度", "受影响市场", "受影响产品", "标题", "摘要", "商业预判", "关键日期", "来源"]


def _write_headers(ws) -> None:
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.fill      = _HEADER_FILL
        cell.font      = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border    = _border()
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def _style_row(ws, row_idx: int, impact: str, parity: int, first_in_group: bool) -> None:
    palette = _IMPACT_PALETTE.get(impact)
    if palette:
        badge_bg, badge_fg, light, dark = palette
        badge_fill = PatternFill(start_color=badge_bg, end_color=badge_bg, fill_type="solid")
        row_fill   = PatternFill(
            start_color=dark if parity % 2 else light,
            end_color=dark if parity % 2 else light,
            fill_type="solid",
        )
    else:
        badge_fill = row_fill = None
        badge_fg   = "000000"

    for col in range(1, len(HEADERS) + 1):
        cell = ws.cell(row=row_idx, column=col)
        cell.border = _border(top_heavy=first_in_group)
        if col == 1:
            cell.alignment = _CENTER
            cell.font      = Font(bold=True, size=11, color=badge_fg)
            if badge_fill:
                cell.fill = badge_fill
        else:
            cell.alignment = _WRAP
            cell.font      = _BODY_FONT
            if row_fill:
                cell.fill  = row_fill


def _fill_sheet(ws, rows) -> None:
    _write_headers(ws)
    prev_impact  = None
    group_parity: dict = {}

    for row in rows:
        impact             = row["impact_level"] or "🟢"
        title_orig         = row["title"] or ""
        title_cn           = row["title_cn"] or ""
        requirement        = row["compliance_requirement"] or ""
        products_display   = row["affected_products_display"] or ""
        markets            = row["affected_markets"] or ""
        deadline           = row["compliance_deadline"]
        key_dates          = row["key_dates"]
        src_url            = row["source_url"] or ""
        fallback_urls_json = row["fallback_urls"] or ""
        worst_case         = row["worst_case_scenario"] or ""
        business_impact    = _IMPORTANCE_NOTE_PAT.sub("", row["business_impact"] or "").strip()
        sources_json       = row["sources"] or ""
        source_institution = row["source_institution"]
        source_language    = row["source_language"]
        try:
            is_synth = bool(row["is_synth"])
        except (IndexError, KeyError):
            is_synth = False

        parity = group_parity.get(impact, 0)
        is_new = impact != prev_impact
        if is_new:
            group_parity[impact] = 0
            parity = 0
        group_parity[impact] = parity + 1

        display_title    = (title_cn or title_orig).strip()
        display_products = products_display.replace("、", "\n")

        # 汇总型法规识别：key_dates 含 ≥3 个 enforcement 时附加"ⓘ含 N 项关键日期"。
        # 标记从原标题尾部移到摘要开头，让标题列保持纯净不被截断。
        summary_indicator = ""
        if key_dates:
            try:
                kd = json.loads(key_dates)
                enf_count = len(kd.get("enforcements") or []) if isinstance(kd, dict) else 0
            except Exception:
                enf_count = 0
            already_summary = any(k in display_title for k in
                                  ("多项要求", "关键日期", "分阶段", "多项义务"))
            if enf_count >= 3 and not already_summary:
                summary_indicator = f"ⓘ含 {enf_count} 项关键日期 ｜ "

        # Stage 4：从所有候选 URL（主 / fallback / sources）选权威分最高的作 hyperlink。
        # 兼顾旧数据（未经 Stage 0 权威排序）和模型自报多源的新数据。
        candidates: list[str] = [src_url] if src_url else []
        if fallback_urls_json:
            try:
                fb = json.loads(fallback_urls_json)
                if isinstance(fb, list):
                    candidates.extend(u for u in fb if isinstance(u, str))
            except Exception:
                pass
        if sources_json:
            try:
                srcs = json.loads(sources_json)
                if isinstance(srcs, list):
                    for s in srcs:
                        if isinstance(s, dict) and s.get("url"):
                            candidates.append(s["url"])
            except Exception:
                pass
        if candidates:
            src_url = authority.best_url(candidates) or src_url

        biz_block = (business_impact or worst_case).strip()
        # 兜底：合成条目的 ⚠️ 标识必须出现在用户可见位置
        # （fallback 路径会写入 business_impact 头部，但若历史数据 / AI 偶发返回空 biz
        #  导致警告丢失，这里基于 sc.full_text 的事实信号补救）
        if is_synth and "AI 合成" not in biz_block:
            biz_block = f"{_SYNTHESIS_WARNING}\n{biz_block}".strip()

        # 摘要列开头加 ⓘ 标识（如有），让标题保持纯净
        requirement_with_indicator = (summary_indicator + requirement) if summary_indicator else requirement

        ws.append([_clean(v) for v in [
            _BADGE_TEXT.get(impact, impact),
            markets,
            display_products,
            display_title,
            requirement_with_indicator,
            biz_block,
            _fmt_dates(deadline, key_dates),
            _fmt_source(source_institution, source_language, title_orig, src_url),
        ]])
        _style_row(ws, ws.max_row, impact, parity, first_in_group=is_new)
        if src_url:
            ws.cell(row=ws.max_row, column=8).hyperlink = src_url

        prev_impact = impact

    last_col = get_column_letter(len(HEADERS))
    ws.auto_filter.ref = f"A1:{last_col}1"


# ── 需人工跟进 sheet ──────────────────────────────────────────────────────────

def _sheet_manual(wb: Workbook) -> None:
    ws = wb.create_sheet("需人工跟进")

    headers = ["标题", "URL", "添加日期"]
    widths  = [46, 56, 14]

    ws.append(headers)
    for i, (cell, w) in enumerate(zip(ws[1], widths), 1):
        cell.fill      = _HEADER_FILL
        cell.font      = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border    = _border()
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

    rows = get_manual_followup()
    alt = [
        PatternFill(start_color="F5F5F5", end_color="F5F5F5", fill_type="solid"),
        PatternFill(start_color="EBEBEB", end_color="EBEBEB", fill_type="solid"),
    ]
    for i, row in enumerate(rows):
        ws.append([_clean(v) for v in [
            row["title"] or "",
            row["source_url"] or "",
            (row["query_date"] or "")[:10],
        ]])
        fill = alt[i % 2]
        for col in range(1, len(headers) + 1):
            cell = ws.cell(row=ws.max_row, column=col)
            cell.fill      = fill
            cell.font      = _BODY_FONT
            cell.border    = _border()
            cell.alignment = _WRAP
        ws.row_dimensions[ws.max_row].height = 36
        if row["source_url"]:
            ws.cell(row=ws.max_row, column=2).hyperlink = row["source_url"]

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"


# ── 主入口 ────────────────────────────────────────────────────────────────────

def _row_apply_priority(row, new_impact: str):
    """把 priority 模块判定的 new_impact 反写到 row（dict 化以便覆盖）。
    保留所有原字段，仅改 impact_level 让 _fill_sheet 按新颜色渲染。"""
    d = dict(row)
    d["impact_level"] = new_impact
    return d


_BILL_NUMBER_RE = re.compile(
    r"\b(?:SB|HB|AB|S\.?B\.?|H\.?B\.?|A\.?B\.?|Senate Bill|House Bill|Assembly Bill|Bill)\s*#?\s*(\d{1,5})\b",
    re.IGNORECASE,
)


def _composite_dedup_key(row):
    """二次去重 key：normalize_reg_id 优先；若失败，尝试"市场 + 法案号"复合 key。

    覆盖场景：
      1) 同一 EU 法规多种 reg_id 写法（normalize_reg_id 已搞定）
      2) 美国州法案 同 SB/AB/HB 编号但 reg_id 文本不同（"California Senate Bill 1271"
         / "CA SB 1271" / "California SB 1271 (SB 1271)"）→ key="美国（加利福尼亚州）|SB1271"
      3) 中国地方条例多次入库（"北京市非机动车管理条例 (修订)" / "新修订的《北京市非
         机动车管理条例》将于5月1日正式施行" / "《北京市非机动车管理条例》修订版实施"）
         → key="中国 (北京)|北京市非机动车管理条例"
    """
    from consolidator import normalize_reg_id
    try:
        reg_id = row["reg_id"]
    except (IndexError, KeyError):
        reg_id = None
    key = normalize_reg_id(reg_id)
    if key:
        return key

    # 兜底：市场 + 法案号
    title = (row["title"] or "").strip()
    title_cn = (row["title_cn"] or "").strip() if "title_cn" in row.keys() else ""
    market = (row["affected_markets"] or "").strip()
    text = title + " " + title_cn

    m = _BILL_NUMBER_RE.search(text)
    if m and market:
        # 提取首个法案前缀字母
        prefix_match = re.search(
            r"\b(SB|HB|AB|Senate Bill|House Bill|Assembly Bill|Bill)\b",
            text, re.IGNORECASE,
        )
        prefix = (prefix_match.group(1) if prefix_match else "BILL").upper()
        prefix = prefix.replace("SENATE BILL", "SB").replace("HOUSE BILL", "HB").replace("ASSEMBLY BILL", "AB")
        return f"BILL/{market}|{prefix}{m.group(1)}"

    # 兜底 2：中国地方条例（"《XXX管理条例》" 模式）
    chinese_reg_match = re.search(r"《([^》]{4,40})》", text)
    if chinese_reg_match and market:
        reg_name = chinese_reg_match.group(1).strip()
        return f"CN_LOCAL/{market}|{reg_name}"

    return None


def _dedup_by_normalized_reg_id(rows_with_score):
    """同 reg_id / 同主题二次去重：每 key 保留 score 最高 1 条。

    入参：[(row, score, new_impact), ...]
    返回：去重后同结构列表。
    """
    seen_keys: dict = {}   # key → (idx_in_keepers, score)
    keepers = []
    for r, score, new_impact in rows_with_score:
        key = _composite_dedup_key(r)
        if not key:
            keepers.append((r, score, new_impact))
            continue
        if key in seen_keys:
            prev_idx, prev_score = seen_keys[key]
            if score > prev_score:
                keepers[prev_idx] = (r, score, new_impact)
                seen_keys[key] = (prev_idx, score)
        else:
            seen_keys[key] = (len(keepers), score)
            keepers.append((r, score, new_impact))
    return keepers


def _final_sort_key(d):
    """周报最终排序：按用户给定优先级
      第一：重要度（new_impact 重染色后：🔴=L1直接 → 🟡=L2L3间接）
      第二：区域（market_tier：0全球→1欧盟→2欧国→3北美→4美联邦→5美州→6加→7加省→8澳新→99其他）
      第三：产品（按 rules/product_sort_order.txt 行序;表外产品 rank=9 排尾）
    """
    impact = d.get("impact_level") or ""
    impact_rank = {"🔴": 1, "🟡": 2, "🟢": 3}.get(impact, 9)
    try:
        mt = d.get("market_tier")
        market_tier = int(mt) if mt is not None else 99
    except (TypeError, ValueError):
        market_tier = 99
    market = d.get("affected_markets") or ""
    products = (d.get("affected_products_display") or "").lower()
    prod_rank = next((r for kw, r in _PRODUCT_RANKS.items() if kw in products), 9)
    return (impact_rank, market_tier, market, prod_rank)


def _filter_and_repaint(rows):
    """两段式过滤渲染（启发式下放后）：

    Stage 1 时间窗粗筛（priority.in_time_window）：
      不在 90 天发布 / 12 月生效窗口 → 剔除（datetime.now() 实时锚定）
      （噪音 / 过程阶段 / 媒体 / 跨主题判定已下放给 LLM 三件套用 few-shot 范本判定,
        不在本层重复硬编码——见 prompts/llm_triage_system.txt / llm_priority_system.txt）

    Stage 2 LLM 终审优先（DB 字段 ai_priority 已写）：
      • ai_priority='P0' + ai_level='L1' → 🔴 进
      • ai_priority='P0' + ai_level='L2' → 🟡 进
      • ai_priority='P1' / 'drop' → 不进信息总览

    Stage 3 LLM 字段缺失时 → priority.score_fallback 兜底（保守不漏球）

    最后：现有 reg_id 去重 + LLM 语义去重 + 排序（不变）
    """
    from analyzer import priority as pri
    from analyzer.llm_dedup import llm_dedup as _llm_dedup

    scored = []
    for r in rows:
        # Stage 1：时间窗粗筛
        if not pri.in_time_window(r):
            continue

        ai_priority = r["ai_priority"] if "ai_priority" in r.keys() else None
        ai_level    = r["ai_level"]    if "ai_level"    in r.keys() else None

        # Stage 2：LLM 终审优先
        if ai_priority == "P0":
            new_impact = "🔴" if ai_level == "L1" else "🟡"
            r_dict = dict(r) if not isinstance(r, dict) else r
            scored.append((r_dict, 100, new_impact))
            continue
        if ai_priority in ("P1", "drop"):
            continue

        # Stage 3：LLM 字段缺失 → 兜底
        res = pri.score_fallback(r)
        if res is None:
            continue
        s, suggested_impact = res
        r_dict = dict(r) if not isinstance(r, dict) else r
        scored.append((r_dict, s, suggested_impact))

    # 现有 reg_id 规则去重 + LLM 语义去重（不变）
    deduped = _dedup_by_normalized_reg_id(scored)
    deduped = _llm_dedup(deduped)
    repainted = [_row_apply_priority(r, ni) for r, _s, ni in deduped]
    repainted.sort(key=_final_sort_key)
    return repainted


def generate_report() -> str:
    """生成周报：保持原 3 sheet 结构 + 原市场排序 + priority 过滤 NOISE/P2。

    sheet 1 - 信息总览：全部 P0+P1（按 重要度→market_tier→市场→产品 排序）
    sheet 2 - 本周更新：近 7 天的 P0+P1（同一排序）
    sheet 3 - 需人工跟进：scraper 抓不到的链接

    原 impact 语义重定义：🔴=L1 直接合规 / 🟡=L2L3 间接合规（去掉 🟢）。
    NOISE（单次召回/个案执法）和 P2（< P1 阈值）不进任何 sheet。
    """
    init_db()
    os.makedirs(REPORTS_DIR, exist_ok=True)

    cutoff    = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    all_rows  = get_all_analyses()
    week_rows = get_week_analyses(cutoff)

    all_filtered  = _filter_and_repaint(all_rows)
    week_filtered = _filter_and_repaint(week_rows)

    wb  = Workbook()
    ws1 = wb.active
    ws1.title = "信息总览"
    _fill_sheet(ws1, all_filtered)
    _fit_sheet(ws1, _MAIN_COL_CAPS)

    ws2 = wb.create_sheet("本周更新")
    _fill_sheet(ws2, week_filtered)
    _fit_sheet(ws2, _MAIN_COL_CAPS)

    _sheet_manual(wb)

    date_str = datetime.now().strftime("%Y年%m月%d日")
    filename = f"GRIS_周报_{date_str}.xlsx"
    filepath = os.path.join(REPORTS_DIR, filename)
    wb.save(filepath)

    n_dropped_all = len(all_rows) - len(all_filtered)
    n_dropped_week = len(week_rows) - len(week_filtered)
    print(f"报告已生成：{filepath}")
    print(f"  信息总览    ：{len(all_filtered)} 条（剔除 {n_dropped_all} 条噪音/低优）")
    print(f"  本周更新    ：{len(week_filtered)} 条（剔除 {n_dropped_week} 条）")
    return filepath


if __name__ == "__main__":
    generate_report()
