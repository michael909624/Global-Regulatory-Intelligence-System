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

import ai_client
import authority
import prompts
from database import get_connection
from utils import get_logger, parse_json_array

_log = get_logger("consolidator")

# Stage 0 LLM 聚类参数
_LLM_CLUSTER_BATCH       = 40   # 单次 LLM 调用最多比较的候选数
_LLM_CLUSTER_MIN_GROUP   = 2    # 启动 LLM 的最小候选数
_JACCARD_PREFILTER_MIN   = 0.25 # token Jaccard 粗筛阈值（< 此值不参与 LLM 比较）

# title 分词停用词——剥离常见法规噪声词后做 Jaccard 粗筛，
# 避免"Decision/Regulation/Notice"等高频词污染相似度
_TITLE_STOPWORDS = {
    "the", "of", "and", "or", "for", "to", "a", "an", "on", "in", "with",
    "act", "regulation", "regulations", "directive", "decision", "decisions",
    "amendment", "amendments", "notice", "order", "rule", "rules",
    "draft", "final", "implementing", "delegated", "consultation",
    "law", "code", "standard", "standards", "guidelines", "guideline",
    "通知", "公告", "决议", "决定", "通告", "法令", "条例", "规定",
}


# ── 导航/非法规 title 识别 ────────────────────────────────────────────────────
# Stage 0 同 reg_id 聚类时，如果成员里混进"高权威域名 + 无关 title"的条目
# （如 eur-lex 域下挂的 "Press Release / Site Map / Glossary"），仅按 authority.score
# 选 keeper 会让真法规被合并到无关 keeper → 主分析按无关 title 判不相关 → 漏召回。
#
# 加这一层"非法规 title"识别，让 keeper 选择优先合规相关的成员。
_NAVIGATION_TITLE_PATTERNS = [
    "press release", "site map", "sitemap", "glossary", "table of contents",
    "about us", "contact us", "cookie policy", "privacy policy",
    "homepage", "home page", "search results", "page not found",
    "annual report", "media inquiries", "communications portal",
    "新闻发布", "网站地图", "术语表", "关于我们", "联系我们", "首页", "搜索结果",
]


def _is_navigation_title(title: str | None) -> bool:
    """识别非法规 title——含导航/通讯/无义务页面关键词。
    Stage 0 keeper 选择时这类 title 排到末尾，避免抢占真法规的 keeper 位。"""
    if not title:
        return True
    t = title.lower()
    return any(p in t for p in _NAVIGATION_TITLE_PATTERNS)


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

# ALIAS → CELEX 反向映射：让别名条目（"CRA"、"AI Act"、"Battery Regulation"）
# 和 CELEX 条目（EU/2024/2847）落到同一聚类 key，根治"同一法规 5 个 reg_id"
# 漏合并问题。优先级高于 _ALIAS_MAP（在 normalize_reg_id 里先匹配）。
_ALIAS_TO_CELEX = {
    r"\bCRA\b|CYBER\s+RESILIENCE\s+ACT":          "EU/2024/2847",
    r"\bAI\s+ACT\b|ARTIFICIAL\s+INTELLIGENCE\s+ACT": "EU/2024/1689",
    r"\bBATTERY\s+REGULATION\b|BATTERIES\s+REGULATION": "EU/2023/1542",
    r"MACHINERY\s+REGULATION":                    "EU/2023/1230",
}

_STD_PREFIXES = ("EN", "IEC", "UL", "ISO", "JIS", "AS/NZS", "AIS", "ANSI", "CSA", "BS")


def normalize_reg_id(raw: str | None) -> str | None:
    """归一化模型自报的 reg_id 到聚类键；返回 None 表示无法归类。

    设计：先尝试抽 CELEX / EU 编号（强信号），命中即归一。这样
    "32024R2847" / "32024R2847 Deadlines" / "32024R2847_Guidance"
    都归到同一 EU/2024/2847；CRA / AI Act 等已知别名也通过
    _ALIAS_TO_CELEX 反向映射到对应 CELEX。
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    # 下划线在 \b 视角是 word char，会破坏词边界检测
    # （"CRA_Guidance" 里 \bCRA\b 不命中）。先转成空格再做正则。
    upper = s.upper().replace("_", " ")

    # CELEX → EU/YYYY/NNN（用 search 而非 match：含后缀/前缀的字符串也能抽出）
    # 支持 4 位或 5 位顺序号：32024R2847 / 32023R1542 / 32024R0900
    m = re.search(r"\b3(\d{4})[RLDC](\d{1,5})\b", upper)
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

    # 别名优先映射到对应 CELEX（让 "CRA" / "AI Act" 等条目和 CELEX 条目同组）
    for pat, celex_key in _ALIAS_TO_CELEX.items():
        if re.search(pat, upper):
            return celex_key

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


# ── LLM 语义聚类（reg_id 正则失效时的兜底）────────────────────────────────────
#
# 设计意图：
#   reg_id 正则只能识别已枚举的格式（CELEX / EU / CFR / GB / 别名…）。
#   遇到 "Basel BC-15/18" / "OECD C(2001)107" 这类未枚举编号，正则
#   会走 RAW/<原值压缩> 兜底——同一法规不同写法（"Decision" vs "Decisions"、
#   带/不带前缀词）会落到不同 RAW key，根本不会被合并。
#
#   这个阶段让 LLM 用语义判断"是否同一法规"——人 1 秒能看出来的，
#   AI 也应该能。每周 1 次低成本调用，根治正则覆盖盲区。
#
# 性能保护：
#   1. token Jaccard 粗筛：不相关条目对不进入 LLM（O(n²) → 实际 O(候选对数)）
#   2. 单次 LLM 调用上限 _LLM_CLUSTER_BATCH 条
#   3. 失败时自动降级：保留原 RAW/ 兜底，不阻断 pipeline


def _title_tokens(title: str | None, reg_id: str | None) -> set[str]:
    """提取标题 + reg_id 的语义 token 集（剥离停用词、标点、大小写）。"""
    text = f"{title or ''} {reg_id or ''}".lower()
    # 拆分：英文按字母/数字串，中文按字符（粗略但够用做粗筛）
    tokens = set()
    for t in re.findall(r"[a-z0-9][a-z0-9\-/]*", text):
        if len(t) >= 2 and t not in _TITLE_STOPWORDS:
            tokens.add(t)
    for ch in re.findall(r"[一-鿿]+", text):
        for c in ch:
            if c not in _TITLE_STOPWORDS:
                tokens.add(c)
    return tokens


def _has_similar_pair(rows: list, min_jaccard: float = _JACCARD_PREFILTER_MIN) -> bool:
    """快速判定该候选组里是否存在至少一对 Jaccard ≥ 阈值——存在才值得调 LLM。"""
    token_sets = [_title_tokens(r["title"], r["reg_id"]) for r in rows]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = token_sets[i], token_sets[j]
            if not a or not b:
                continue
            inter = len(a & b)
            union = len(a | b)
            if union and inter / union >= min_jaccard:
                return True
    return False


def _llm_cluster_residual(rows: list, business_scope: str) -> list[list[int]]:
    """对 RAW/ 兜底的残余候选调一次 LLM 做语义聚类。
    返回 [[id, id, ...], ...]，每组至少 2 个 ID。失败时返回 []。
    """
    if len(rows) < _LLM_CLUSTER_MIN_GROUP:
        return []
    if not _has_similar_pair(rows):
        # 候选两两都不相似——LLM 大概率也找不出合并，省一次调用
        return []

    entries = ""
    valid_ids: set[int] = set()
    for r in rows[:_LLM_CLUSTER_BATCH]:
        valid_ids.add(r["id"])
        title  = (r["title"] or "")[:120]
        reg_id = (r["reg_id"] or "—")[:60]
        market = (r["market"] or "—")[:30]
        snip   = (r["snippet"] or "").replace("\n", " ")[:80]
        entries += f"[{r['id']}] {title} | {reg_id} | {market} | {snip}\n"

    system = prompts.load("cluster_residual_system").format(business_scope=business_scope)
    prompt = prompts.load("cluster_residual").format(n=len(valid_ids), entries=entries)

    try:
        # Stage 0 LLM 聚类 — 语义判断"条目 A 和 B 是不是同一法规"。
        # 候选已用 Jaccard 粗筛过滤，进 LLM 的都是高度相似的 token 集，
        # lite + 关 thinking 完全胜任，flash 是浪费。
        resp = ai_client.call_json(
            prompt, system=system,
            model="gemini-2.5-flash-lite",
            thinking_budget=0,
        )
    except Exception as e:
        _log.warning("Stage 0 LLM cluster failed: %s", e)
        return []

    parsed = parse_json_array(resp) or []
    clusters: list[list[int]] = []
    seen: set[int] = set()
    for g in parsed:
        if not isinstance(g, dict):
            continue
        gids = g.get("group_ids") or []
        if not isinstance(gids, list) or len(gids) < 2:
            continue
        # 校验：所有 ID 都在本批次、且未在之前的组里出现过
        if not all(isinstance(i, int) and i in valid_ids for i in gids):
            continue
        if any(i in seen for i in gids):
            continue
        seen.update(gids)
        reason = (g.get("reason") or "")[:120]
        _log.info("STAGE0-LLM cluster ids=%s reason=%s", gids, reason)
        clusters.append(gids)
    return clusters


def _apply_clusters(rows_by_id: dict, clusters: list[list[int]], verbose: bool) -> tuple[int, int]:
    """对 LLM 输出的聚类执行合并（与 reg_id 合并相同的 keeper / consolidated_into 机制）。"""
    groups_merged = rows_merged = 0
    for gids in clusters:
        members = [rows_by_id[i] for i in gids if i in rows_by_id]
        if len(members) < 2:
            continue

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
        ranked = authority.sort_by_authority(all_urls)
        primary_url = ranked[0] if ranked else (members[0]["source_url"] or "")
        fallback_list = ranked[1:6]
        fallback_json = json.dumps(fallback_list, ensure_ascii=False) if fallback_list else None

        keeper = max(
            members,
            key=lambda r: (
                authority.score(r["source_url"] or ""),
                1 if (r["source_url"] or "") == primary_url else 0,
                -r["id"],
            ),
        )
        merged = [m for m in members if m["id"] != keeper["id"]]

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
        if verbose:
            sample = " / ".join((m["title"] or "")[:30] for m in members[:3])
            print(f"  ✓ [LLM 语义] keep={keeper['id']} merge {len(merged)} 条  [{sample}]")
    return groups_merged, rows_merged


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
    already_merged_ids: set[int] = set()  # 给 LLM 阶段：本轮已被 reg_id 合并的从条目

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

        # 主条目选择：优先非导航 title（防止"高权威域名 + 无关 title"抢占 keeper），
        # 其次权威分，再次 primary URL 匹配，最后 ID 最小者
        keeper = max(
            members,
            key=lambda r: (
                0 if _is_navigation_title(r["title"]) else 1,
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
        already_merged_ids.update(m["id"] for m in merged)
        _log.info(
            "STAGE0 key=%s keeper=%d merged=%s primary_url=%s",
            key, keeper["id"], [m["id"] for m in merged], primary_url[:80],
        )
        if verbose:
            sample = " / ".join((m["title"] or "")[:30] for m in members[:3])
            print(f"  ✓ [{key}] keep={keeper['id']} merge {len(merged)} 条  [{sample}]")

    # ── LLM 语义聚类：对 RAW/ 兜底键 + 无 reg_id 的残余候选再扫一遍 ───────
    # reg_id 正则只能识别已枚举编号格式；遇到 "Basel BC-15/18" 这类
    # 非标准编号会落入 RAW/<原值>，同一法规不同写法 key 不同 → 漏合并。
    # LLM 用语义判断"是否同一法规"——人 1 秒能看出来的，AI 也应该能。
    residual_ids: set[int] = {r["id"] for r in no_key}
    for k, members in groups.items():
        if k.startswith("RAW/"):
            residual_ids.update(m["id"] for m in members)
    residual_ids -= already_merged_ids   # 已被 reg_id 合并的从条目不参与
    residual = [r for r in rows if r["id"] in residual_ids]

    llm_groups = llm_rows = 0
    if len(residual) >= _LLM_CLUSTER_MIN_GROUP:
        if verbose:
            print(f"\n  ── Stage 0+：LLM 语义聚类（{len(residual)} 条残余候选）──")
        # 引入 BUSINESS_SCOPE 作为系统判定基础
        from analyzer._shared import BUSINESS_SCOPE
        clusters = _llm_cluster_residual(residual, BUSINESS_SCOPE)
        if clusters:
            rows_by_id = {r["id"]: r for r in residual}
            llm_groups, llm_rows = _apply_clusters(rows_by_id, clusters, verbose)
            groups_merged += llm_groups
            rows_merged   += llm_rows
        elif verbose:
            print(f"  LLM 判定：无可合并语义组")

    untouched = len(rows) - rows_merged

    if verbose:
        if groups_merged:
            extra = f"（其中 LLM 合并 {llm_groups} 组 / {llm_rows} 条）" if llm_groups else ""
            print(f"\n  完成：{groups_merged} 组合并 {rows_merged} 条 → 留 {untouched} 条独立走 scrape {extra}")
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
