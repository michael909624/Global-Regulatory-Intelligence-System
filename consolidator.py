"""
Stage 0：法规编号聚类（reg_id consolidator）。

设计意图：
  在 scrape 之前对 raw_search_results 做软合并 —— 同一法规的多条候选
  归并为一条主条目，剩余条目标记 consolidated_into 后跳过 scrape/analyze。

为什么是"软合并"（保留行而不是 DELETE）：
  • 复盘价值：可以反查"模型在哪些 reg_id 上重复最严重"，反馈给 prompt 改进
  • 可逆：发现误合并时，把 consolidated_into 置 NULL 即可恢复
  • 对历史 schema 友好：不动 scraped_content / compliance_analysis 的外键

为什么不调 LLM：
  • Stage 0 只做"明确同一编号"的合并 —— 纯字符串处理，确定性，零成本
  • 跨编号的语义合并（CRA 与 32024R2847、Reg 2023/1542 的不同子条款）
    是 Stage 3 LLM 的职责，不属于 Stage 0
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import authority
from database import get_connection
from utils import get_logger

_log = get_logger("consolidator")


# ── reg_id 归一化 ──────────────────────────────────────────────────────────────
#
# 把模型自报的多种写法折叠到统一的聚类键。例：
#   "(EU) 2024/2847"             → "EU/2024/2847"
#   "Regulation (EU) 2024/2847"  → "EU/2024/2847"
#   "32024R2847" (CELEX)         → "EU/2024/2847"     ← 跨格式合并
#   "GB 17761"                   → "GB/17761"
#   "16 CFR Part 1273"           → "CFR/16-1273"
#   "UN R155"                    → "UN/R155"
#   "CRA" / "Cyber Resilience Act" → "ALIAS/CRA"
#
# 保守原则：只折叠"明确同一编号的不同写法"。"CRA" 与 "EU/2024/2847"
# 在 Stage 0 视作不同组（两者通过 ALIAS/EU 路径独立归一） —— 跨家族合并交给 Stage 3。

_ALIAS_MAP = {
    r"\bCRA\b|CYBER\s+RESILIENCE\s+ACT":          "CRA",
    r"\bAI\s+ACT\b":                              "AI_ACT",
    r"\bBATTERY\s+REGULATION\b":                  "BATTERY_REG",
    r"\bPSTI\b|PRODUCT\s+SECURITY\s+(?:&|AND)\s+TELECOM": "PSTI",
    r"\bRO?HS\b":                                 "ROHS",
    r"\bREACH\b":                                 "REACH",
    r"\bGDPR\b":                                  "GDPR",
    # 危险品运输标准——版本号差异不视作独立法规
    r"\bIATA\b(?:\s+DGR|\s+DANGEROUS\s+GOODS)?":  "IATA_DGR",
    r"\bIMDG\b":                                  "IMDG_CODE",
    r"\bADR\b(?:[/\s]+RID)?":                     "ADR",
    r"\bDOT\b\s*(?:HMR|49\s*CFR)":                "US_HMR",
    r"\bPHMSA\b":                                 "US_HMR",
}

_STD_PREFIXES = ("EN", "IEC", "UL", "ISO", "JIS", "AS/NZS", "AIS", "ANSI", "CSA", "BS")


def normalize_reg_id(raw: str | None) -> str | None:
    """归一化模型自报的 reg_id 到聚类键；返回 None 表示无法归类。"""
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    upper = s.upper()

    # CELEX → EU/YYYY/NNN
    m = re.match(r"^3(\d{4})[RLDC](\d{4})$", upper)
    if m:
        return f"EU/{m.group(1)}/{int(m.group(2))}"

    # (EU) YYYY/NNN  /  Reg YYYY/NNN  /  Directive YYYY/NNN
    m = re.search(r"\(EU\)\s*(\d{4})/(\d+)", upper)
    if m:
        return f"EU/{m.group(1)}/{int(m.group(2))}"
    m = re.search(
        r"\b(?:REG|REGULATION|DIRECTIVE|DECISION|DELEGATED|IMPLEMENTING)"
        r"[A-Z\s\(\)]*?(\d{4})/(\d+)",
        upper,
    )
    if m:
        return f"EU/{m.group(1)}/{int(m.group(2))}"

    # 美国 CFR
    m = re.search(r"\b(\d{1,3})\s*CFR\s*(?:PART\s*)?(\d+)", upper)
    if m:
        return f"CFR/{m.group(1)}-{m.group(2)}"
    m = re.search(r"\bCFR\s*PART\s*(\d+)", upper)
    if m:
        return f"CFR/0-{m.group(1)}"

    # 中国 GB / GB/T
    m = re.search(r"\bGB[/\s\-]?T?[\s\-]?(\d{4,6})", upper)
    if m:
        return f"GB/{m.group(1)}"

    # UN / UNECE Regulation
    m = re.search(
        r"\bUN\s*(?:ECE\s*)?R(?:EGULATION)?\s*(?:NO\.?)?\s*(\d+)",
        upper,
    )
    if m:
        return f"UN/R{m.group(1)}"

    # 国际标准（EN / IEC / UL / ISO / JIS / AS/NZS / AIS / ANSI / CSA / BS）
    for prefix in _STD_PREFIXES:
        pat = re.compile(rf"\b{re.escape(prefix)}[/\s\-]?(\d{{3,6}})", re.I)
        m = pat.search(upper)
        if m:
            key_prefix = prefix.upper().replace("/", "_")
            return f"{key_prefix}/{m.group(1)}"

    # 别名（CRA / AI Act / Battery Regulation / PSTI / RoHS / REACH / GDPR）
    for pat, alias in _ALIAS_MAP.items():
        if re.search(pat, upper):
            return f"ALIAS/{alias}"

    # 兜底：原值压缩作为弱聚类键（保留模型独有的奇怪编号）
    fallback = re.sub(r"[^\w\d/\-]", "", s.lower())
    if len(fallback) >= 4:
        return f"RAW/{fallback[:50]}"
    return None


# ── Stage 0 主流程 ────────────────────────────────────────────────────────────

def consolidate_pending(verbose: bool = True) -> tuple[int, int, int]:
    """
    对 scrape_status='待抓取' AND consolidated_into IS NULL 的条目按 reg_id 聚类。

    返回 (groups_merged, rows_merged, untouched)：
      groups_merged  发生合并的组数
      rows_merged    被标记 consolidated_into 的条目数（不再独立走 scrape）
      untouched      无 reg_id 或独占 reg_id，原样保留
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title, reg_id, source_url, fallback_urls, market, snippet
            FROM raw_search_results
            WHERE scrape_status = '待抓取'
              AND consolidated_into IS NULL
        """).fetchall()

    if not rows:
        if verbose:
            print("  Stage 0：无待处理条目")
        return 0, 0, 0

    # 按归一化 reg_id 分组；无法归类的保留独立
    groups: dict[str, list] = defaultdict(list)
    no_key: list = []
    for r in rows:
        key = normalize_reg_id(r["reg_id"])
        if key is None:
            no_key.append(r)
        else:
            groups[key].append(r)

    mergeable_keys = [k for k, v in groups.items() if len(v) >= 2]
    keep_alone = sum(1 for v in groups.values() if len(v) == 1)

    if verbose:
        print(f"\n  ── Stage 0：法规编号聚类 ──")
        print(f"  待处理 {len(rows)} 条")
        print(f"    ├ 有 reg_id 可归类      ：{len(rows) - len(no_key)}")
        print(f"    └ 无 reg_id（留给 Stage 3）：{len(no_key)}")
        print(f"  分到 {len(groups)} 组；其中可合并组（≥2 条）{len(mergeable_keys)}，独占组 {keep_alone}")

    groups_merged = 0
    rows_merged = 0

    for key in mergeable_keys:
        members = groups[key]

        # 候选 URL：所有成员的 source_url + fallback_urls
        all_urls: list[str] = []
        for m in members:
            if m["source_url"]:
                all_urls.append(m["source_url"])
            if m["fallback_urls"]:
                try:
                    fb = json.loads(m["fallback_urls"])
                    if isinstance(fb, list):
                        all_urls.extend(u for u in fb if isinstance(u, str))
                except Exception:
                    pass

        # 按权威度去重排序 → 主 URL + fallback
        ranked = authority.sort_by_authority(all_urls)
        primary_url = ranked[0] if ranked else (members[0]["source_url"] or "")
        fallback_list = ranked[1:6]
        fallback_json = json.dumps(fallback_list, ensure_ascii=False) if fallback_list else None

        # 主条目 = source_url 与 primary_url 匹配的成员，或权威分最高的成员
        keeper = max(
            members,
            key=lambda r: (
                authority.score(r["source_url"] or ""),
                1 if (r["source_url"] or "") == primary_url else 0,
                -r["id"],
            ),
        )
        merged = [m for m in members if m["id"] != keeper["id"]]

        # 合并 markets
        markets: list[str] = []
        for m in members:
            for part in (p.strip() for p in (m["market"] or "").split("、") if p.strip()):
                if part not in markets:
                    markets.append(part)
        merged_market = "、".join(markets)

        with get_connection() as conn:
            conn.execute(
                "UPDATE raw_search_results "
                "SET source_url=?, fallback_urls=?, market=? WHERE id=?",
                (primary_url, fallback_json, merged_market, keeper["id"]),
            )
            for m in merged:
                conn.execute(
                    "UPDATE raw_search_results "
                    "SET consolidated_into=?, scrape_status='已抓取' WHERE id=?",
                    (keeper["id"], m["id"]),
                )

        groups_merged += 1
        rows_merged += len(merged)
        _log.info(
            "STAGE0 key=%s keeper=%d merged=%s primary_url=%s",
            key, keeper["id"], [m["id"] for m in merged], primary_url[:80],
        )
        if verbose:
            sample = " / ".join((m["title"] or "")[:30] for m in members[:3])
            print(f"  ✓ [{key}] keep={keeper['id']} merge {len(merged)} 条  [{sample}]")

    untouched = len(rows) - rows_merged

    if verbose:
        if groups_merged:
            print(f"\n  完成：{groups_merged} 组合并 {rows_merged} 条 → 留 {untouched} 条独立走 scrape")
        else:
            print(f"  完成：无可合并组")

    return groups_merged, rows_merged, untouched


def run_consolidation_command() -> None:
    """CLI 入口：python gris.py consolidate"""
    g, r, u = consolidate_pending(verbose=True)
    if g:
        print(f"\n  最终：{g} 组，{r} 条被合并；{u} 条原样保留待 scrape。")


if __name__ == "__main__":
    run_consolidation_command()
