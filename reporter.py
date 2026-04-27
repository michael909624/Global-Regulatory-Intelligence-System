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

from config import REPORTS_DIR
from database import init_db, get_all_analyses, get_week_analyses, get_manual_followup

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

_MAIN_COL_CAPS   = [5, 16, 32, 42, 52, 52, 34, 52]
_MANUAL_COL_CAPS = [50, 70, 12]


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

def _fmt_dates(pub: str | None, deadline: str | None, key_dates_json: str | None) -> str:
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

    te = kd.get("transition_end")
    if te and te not in seen:
        parts.append(f"强制日：{te}")
        seen.add(te)

    if kd.get("consultation_close") and kd["consultation_close"] not in seen:
        parts.append(f"咨询截止：{kd['consultation_close']}")

    # 极旧记录 fallback
    if not parts:
        if pub:      parts.append(f"发布日：{pub[:10]}")
        if deadline: parts.append(f"截止日：{deadline}")

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

HEADERS = ["重要度", "受影响产品", "受影响市场", "标题", "摘要", "商业预判", "关键日期", "来源"]


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
        pub_date           = row["publish_date"]
        deadline           = row["compliance_deadline"]
        key_dates          = row["key_dates"]
        src_url            = row["source_url"] or ""
        worst_case         = row["worst_case_scenario"] or ""
        business_impact    = row["business_impact"] or ""
        sources_json       = row["sources"] or ""
        source_institution = row["source_institution"]
        source_language    = row["source_language"]

        parity = group_parity.get(impact, 0)
        is_new = impact != prev_impact
        if is_new:
            group_parity[impact] = 0
            parity = 0
        group_parity[impact] = parity + 1

        display_title    = (title_cn or title_orig).strip()
        display_products = products_display.replace("、", "\n")

        if sources_json:
            try:
                srcs = json.loads(sources_json)
                if srcs and isinstance(srcs, list) and srcs[0].get("url"):
                    src_url = srcs[0]["url"]
            except Exception:
                pass

        ws.append([_clean(v) for v in [
            _BADGE_TEXT.get(impact, impact),
            display_products,
            markets,
            display_title,
            requirement,
            (business_impact or worst_case).strip(),
            _fmt_dates(pub_date, deadline, key_dates),
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

def generate_report() -> str:
    init_db()
    os.makedirs(REPORTS_DIR, exist_ok=True)

    cutoff    = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    all_rows  = get_all_analyses()
    week_rows = get_week_analyses(cutoff)

    wb  = Workbook()
    ws1 = wb.active
    ws1.title = "合规情报总览"
    _fill_sheet(ws1, all_rows)
    _fit_sheet(ws1, _MAIN_COL_CAPS)

    ws2 = wb.create_sheet("本周新增")
    _fill_sheet(ws2, week_rows)
    _fit_sheet(ws2, _MAIN_COL_CAPS)

    _sheet_manual(wb)

    date_str = datetime.now().strftime("%Y年%m月%d日")
    filename = f"GRIS_周报_{date_str}.xlsx"
    filepath = os.path.join(REPORTS_DIR, filename)
    wb.save(filepath)

    print(f"报告已生成：{filepath}")
    print(f"  合规情报总览：{len(all_rows)} 条")
    print(f"  本周新增    ：{len(week_rows)} 条")
    return filepath


if __name__ == "__main__":
    generate_report()
