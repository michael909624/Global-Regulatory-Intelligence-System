"""
Stage 3 整合去重：在主分析与降级合成都跑完之后，对 compliance_analysis 二次合并。

两 Pass 策略：
  Pass 1：按 normalize_reg_id 跨域确定性合并（不调 LLM）
          —— 同一 reg_id 的不同子条款 / 语言版本 / 实施细则切片直接合并
  Pass 2：剩余条目按 (sorted business_dimensions, affected_products) 分组，
          调 LLM 做语义合并 —— 维度同质度高 → 调用 ROI 提升

选 keeper 的优先级：
  1. 非合成条目优先（合成路径带 ⚠️，可信度低）
  2. 来源域名权威分高优先（authority.score）
  3. id 较小优先（保留早入库的）

注：与 top-level consolidator.py（Stage 0：在 scrape 前对 raw_search_results
软合并）是不同阶段，命名相近但不要混淆。
"""
from __future__ import annotations

import concurrent.futures
import json
import re
from collections import defaultdict

import ai_client
import authority
import prompts
from database import get_connection
from utils import get_logger, parse_json_array, normalize_reg_id

from ._shared import BUSINESS_SCOPE

_log = get_logger("analyzer")

# 并发参数
_CONSOLIDATE_WORKERS = 3   # consolidation LLM 调用

# 工程常量(非业务规则);改动需评估对 reg_id 抽取兜底的影响,请勿外移。
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

_CONSOLIDATION_SYSTEM      = prompts.load("consolidation_system").format(
    business_scope=BUSINESS_SCOPE,
)
_CONSOLIDATION_PROMPT_TMPL = prompts.load("consolidation")

# 工程常量(非业务规则);改动需评估对 Pass 2 拒收过度合并的影响,请勿外移。
# 矛盾措辞检测——LLM 自报"尽管/侧重点不同/分别关注/一项..另一项"等措辞时，
# reason 本身就承认候选有实质差异，按"维度独立"原则不应合并。
# 这是 Pass 2 分组放宽（dims 软聚类）后的副作用兜底——LLM 候选范围变大
# 后倾向积极合并，事后过滤掉自相矛盾的 reason。
_CONTRADICTION_RE = re.compile(
    r"尽管|虽然|侧重点不同|分别关注|分别针对|分别涉及|"
    r"一项[关聚针描](?:注|焦|对|述)|另一项|"
    r"although|despite|however|whereas",
    re.IGNORECASE,
)


def _is_contradictory(reason: str | None) -> bool:
    """reason 含矛盾措辞 → 视为过度合并嫌疑，应拒收。"""
    return bool(reason and _CONTRADICTION_RE.search(reason))

_CONSOLIDATION_QUERY = """
    SELECT ca.id, rs.title, rs.source_url, rs.reg_id, ca.affected_markets,
           ca.impact_level, ca.affected_products, ca.business_dimensions,
           sc.full_text, ca.compliance_requirement,
           ca.key_dates, ca.worst_case_scenario, ca.business_impact,
           ca.source_institution, ca.source_language, ca.sources,
           ca.compliance_deadline
    FROM compliance_analysis ca
    JOIN scraped_content sc ON sc.id = ca.scraped_id
    JOIN raw_search_results rs ON rs.id = sc.raw_id
    WHERE rs.consolidated_into IS NULL
    ORDER BY ca.id
"""


def _merge_markets(markets_list: list[str]) -> str:
    seen: list[str] = []
    for m in markets_list:
        for part in (p.strip() for p in (m or "").split("、") if p.strip()):
            if part not in seen:
                seen.append(part)
    return "、".join(seen)


def _row_get(row, key: str, default=None):
    """安全读 sqlite3.Row 字段（缺列时返回 default）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _merge_dimensions(rows) -> str:
    """合并多条记录的 business_dimensions JSON 数组（去重保序）。"""
    seen: list[str] = []
    for r in rows:
        try:
            arr = json.loads(_row_get(r, "business_dimensions") or "[]")
        except Exception:
            arr = []
        if not isinstance(arr, list):
            continue
        for d in arr:
            if isinstance(d, str) and d not in seen:
                seen.append(d)
    return json.dumps(seen, ensure_ascii=False)


def _merge_key_dates(rows) -> str:
    """合并多条记录的 key_dates JSON：
       publish/effective/consultation_close 取首条非空；
       enforcements 按 (date, scope) 去重并集——不同子条款各自的强制日全保留。
    """
    publish = effective = consultation = None
    enforcements: list = []
    seen_enf: set = set()
    for r in rows:
        try:
            kd = json.loads(_row_get(r, "key_dates") or "{}")
        except Exception:
            kd = {}
        if not isinstance(kd, dict):
            continue
        if not publish and kd.get("publish"):
            publish = kd["publish"]
        if not effective and kd.get("effective"):
            effective = kd["effective"]
        if not consultation and kd.get("consultation_close"):
            consultation = kd["consultation_close"]
        for e in (kd.get("enforcements") or []):
            if not isinstance(e, dict):
                continue
            sig = ((e.get("date") or "").strip(), (e.get("scope") or "").strip())
            if any(sig) and sig not in seen_enf:
                seen_enf.add(sig)
                enforcements.append(e)
    return json.dumps({
        "publish":            publish,
        "effective":          effective,
        "enforcements":       enforcements,
        "consultation_close": consultation,
    }, ensure_ascii=False)


def _merge_sources(rows) -> str | None:
    """合并所有 sources JSON 数组（按 url 去重，保序）。返回 None 表示无变化。"""
    keeper_sources_raw = _row_get(rows[0], "sources")
    try:
        merged: list = json.loads(keeper_sources_raw or "[]")
    except Exception:
        merged = []
    if not isinstance(merged, list):
        merged = []
    seen_urls = {s["url"] for s in merged if isinstance(s, dict) and s.get("url")}
    changed = False
    for r in rows[1:]:
        try:
            other = json.loads(_row_get(r, "sources") or "[]")
        except Exception:
            other = []
        if not isinstance(other, list):
            continue
        for s in other:
            if isinstance(s, dict) and s.get("url") and s["url"] not in seen_urls:
                seen_urls.add(s["url"])
                merged.append(s)
                changed = True
    if not changed:
        return None
    return json.dumps(merged, ensure_ascii=False)


def _backfill_keeper_text_fields(keeper, to_del) -> dict:
    """keeper 缺失而被删条目里有的文本字段，回补到 keeper（保留先发现非空值）。"""
    patch: dict = {}
    for col in (
        "compliance_requirement", "worst_case_scenario", "business_impact",
        "source_institution", "source_language", "compliance_deadline",
    ):
        cur = (_row_get(keeper, col) or "")
        if isinstance(cur, str):
            cur = cur.strip()
        if cur:
            continue
        for r in to_del:
            v = _row_get(r, col)
            if isinstance(v, str):
                v = v.strip()
            if v:
                patch[col] = v
                break
    return patch


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

        # 合并需要"并集"语义的字段——丢任何一个被删条目独有的信号
        # 都意味着报告里某条同 reg_id 法规的 dim/强制日/sources 永久消失。
        merged_market = _merge_markets([r["affected_markets"] or "" for r in group])
        merged_dims   = _merge_dimensions(group)
        merged_dates  = _merge_key_dates(group)
        merged_srcs   = _merge_sources(group)
        backfill_patch = _backfill_keeper_text_fields(keep, to_del)

        del_ids = [r["id"] for r in to_del]

        update_cols: list[str] = [
            "affected_markets=?", "business_dimensions=?", "key_dates=?",
        ]
        update_vals: list = [merged_market, merged_dims, merged_dates]
        if merged_srcs is not None:
            update_cols.append("sources=?")
            update_vals.append(merged_srcs)
        for col, val in backfill_patch.items():
            update_cols.append(f"{col}=?")
            update_vals.append(val)
        update_vals.append(keep_id)
        update_sql = (
            f"UPDATE compliance_analysis SET {', '.join(update_cols)} WHERE id=?"
        )

        with get_connection() as conn:
            conn.execute(update_sql, update_vals)
            for did in del_ids:
                conn.execute("DELETE FROM compliance_analysis WHERE id=?", (did,))
                deleted_entries += 1

        merged_groups += 1
        titles = " / ".join((r["title"] or "")[:35] for r in group)
        _log.info(
            "REGID-DEDUP key=%s keep=%d deleted=%s backfilled=%s",
            key, keep_id, del_ids, sorted(backfill_patch.keys()),
        )
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
        except Exception as e:
            _log.warning("dim parse fail id=%d: %s", r["id"], e)
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
        # Stage 3 Pass 2 候选已按 (产品, 重要度, dims) 预分组——高度相似的条目
        # 进入 LLM 时 LLM 只需做"是否同一法规"的二元判断，不需要复杂多步推理。
        # 用 lite + 关 thinking 节省成本；下游 _is_contradictory 会兜底拒收
        # LLM 自相矛盾的合并，所以 lite 偶尔误判不会污染数据。
        resp_text = ai_client.call_json(
            prompt, system=_CONSOLIDATION_SYSTEM,
            model="gemini-2.5-flash-lite",
            thinking_budget=0,
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

        # Pass 2 兜底：LLM 自己说出"尽管/侧重点不同/分别关注..."的合并大概率
        # 是过度积极。reason 自相矛盾就拒收——按"维度独立"原则保留独立条目。
        if _is_contradictory(reason):
            _log.info("REJECT contradictory merge group=%s reason=%s", gids, reason)
            print(f"  ⊘ 拒收矛盾合并：{reason[:60]}")
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


def run_consolidation() -> tuple[int, int]:
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

    # ── Pass 2：分组策略 ──────────────────────────────────────────────────
    # 旧版用 (sorted dims, products) 严格相等才同组——dims 集合差一个元素就分流，
    # 同一法规两条记录（dims=["IMPORT","EOL"] vs ["ENFORCE","EOL","IMPORT"]）
    # 进不了同一 LLM batch，错失合并（Basel 历史漏判根因）。
    #
    # 新版改为两步：
    #   Step 1：按 (products, impact) 初步分组（产品/重要度差异属"独立法规"信号）
    #   Step 2：同子组内对 dims 用 connected components 软聚类——
    #           dims 集合有交集即视为同候选池（"主维度重叠"判断）。
    # 这样 dims 完全相等仍然同组，dims 略有差异（高度重叠）也合并到同 batch
    # 让 LLM 判定，dims 完全不相干（如 RD vs USE）才分到不同 batch。
    def _parse_dims(r) -> set[str]:
        try:
            arr = json.loads(r["business_dimensions"] or "[]")
            return {d for d in arr if isinstance(d, str)} if isinstance(arr, list) else set()
        except Exception as e:
            _log.warning("dim parse fail id=%d: %s", r["id"], e)
            return set()

    def _dim_components(members: list) -> list[list]:
        """同 (products, impact) 子组内按 dims 交集做 connected components。"""
        n = len(members)
        parent = list(range(n))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        dim_sets = [_parse_dims(m) for m in members]
        for i in range(n):
            for j in range(i + 1, n):
                a, b = dim_sets[i], dim_sets[j]
                # 至少一方无 dim → 视为待合并候选（让 LLM 判断）；都非空 → 看交集
                if not a or not b or (a & b):
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[ra] = rb
        clusters: dict = defaultdict(list)
        for i in range(n):
            clusters[find(i)].append(members[i])
        return list(clusters.values())

    prelim_groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if (r["affected_products"] or "") == "不相关":
            continue
        products = r["affected_products"] or "未知"
        impact   = r["impact_level"] or "?"
        prelim_groups[(products, impact)].append(r)

    topic_groups: dict[tuple, list] = {}
    for (products, impact), members in prelim_groups.items():
        if len(members) < 2:
            continue
        for c_idx, cluster in enumerate(_dim_components(members)):
            if len(cluster) < 2:
                continue
            # 用簇内 dims 并集作为 topic_hint 显示
            dims_union = sorted({d for m in cluster for d in _parse_dims(m)})
            dims_label = "/".join(dims_union) if dims_union else "无维度"
            topic_groups[(dims_label, products, impact, c_idx)] = cluster

    all_ids       = {r["id"] for r in rows}
    merged_total  = pre_m
    deleted_total = pre_d

    # 收集所有 LLM 任务（每个 batch 一次调用），并发执行
    # 注：不同 (产品, 重要度, dim 簇) 组的 ca.id 互斥，分批 slice 也互斥 → 无 DELETE 冲突
    tasks: list[tuple[list, str]] = []
    for key, grp in topic_groups.items():
        if len(grp) < 2:
            continue
        dims_label, products, impact, _c_idx = key
        topic_hint = f"维度={dims_label} / 产品={products} / 重要度={impact}"
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
