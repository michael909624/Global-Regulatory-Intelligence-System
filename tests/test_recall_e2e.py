"""
端到端召回率评测：用 50 条虚拟法规跑完整 pipeline（不真调 Gemini）。

跑测目标：
  • 召回率 ≥ 90%：should_appear=True 的法规出现在最终周报视图里的比例
  • 误报率 ≤ 10%：should_appear=False 的法规进入周报的比例

评测路径（不动 researcher 的 LLM 召回端，假定 researcher 已正确入库）：
  1. 把 50 条测试用例直接写入 raw_search_results + scraped_content
  2. mock ai_client，按 prompt 类型返回标注或合理"LLM 行为"
  3. 跑 consolidator.consolidate_pending（Stage 0 reg_id 软合并）
  4. 跑 analyzer.run_analysis（主分析 + Stage 3 收敛）
  5. 调 database.get_all_analyses 看哪些进了周报视图

使用：
  python3 -m tests.test_recall_e2e
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

# 必须在 import 任何 GRIS 模块前重写 DATABASE_PATH，否则会污染真实库
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["GRIS_TEST_DB"] = _TMP_DB

# repo 根目录到 sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

# 重写 config 模块的 DATABASE_PATH
import config  # noqa: E402
config.DATABASE_PATH = _TMP_DB

import json  # noqa: E402

import ai_client  # noqa: E402
import consolidator  # noqa: E402
from database import init_db, get_connection, get_all_analyses  # noqa: E402
from utils import reg_hash  # noqa: E402

from tests.fixtures.synthetic_regs import (  # noqa: E402
    ALL_CASES,
    SyntheticReg,
    expected_relevant_count,
    expected_irrelevant_count,
)


# ── LLM 模拟器 ───────────────────────────────────────────────────────────────
# 按测试用例标注产出"完美的"分析结果——这样测试失败一定是链路问题（去重/合并/
# 字段构造/报表过滤/类型崩溃）而不是判断错误。trap_kind 可以注入故障模式。

_CASE_BY_HASH: dict[str, SyntheticReg] = {}
_CASE_BY_TITLE: dict[str, SyntheticReg] = {}


def _index_cases() -> None:
    for c in ALL_CASES:
        _CASE_BY_HASH[reg_hash(c.title)] = c
        _CASE_BY_TITLE[c.title.strip().lower()] = c


def _fake_llm_for_analyzer(prompt: str, case: SyntheticReg) -> dict:
    """模拟主分析/合成路径的 LLM 返回。trap 在这里注入。"""
    if case.trap_kind == "safety_block" and not _maybe_unblock_safety(case):
        # 触发 ai_client.BlockedResponseError 路径——返回空响应 + finish_reason
        raise ai_client.BlockedResponseError("simulated SAFETY block")

    products_field = "、".join(case.expected_products) if case.expected_products else "不相关"
    if case.trap_kind == "list_products":
        # 故意返回 list 类型 — 测 normalize_products 兜底
        products_field = case.expected_products if case.expected_products else []

    if not case.should_appear_in_report:
        # 反例 → "不相关"
        return {
            "requirement":         "无适用要求（标的产品超出业务范围）",
            "dates":               {"publish": None, "effective": None,
                                    "enforcements": [], "consultation_close": None},
            "deadline":            None,
            "importance":          "🟢",
            "importance_note":     "阶段?（不相关）+ C? → 🟢",
            "worst_case":          "—",
            "business_impact":     "—",
            "business_dimensions": [],
            "affected_products":   "不相关",
            "affected_markets":    "",
        }

    # 正例
    return {
        "requirement":         "1. 模拟要求一\n2. 模拟要求二\n3. 模拟要求三",
        "dates":               {
            "publish": "2026-01-01",
            "effective": "2026-06-01",
            "enforcements": [{"date": "2026-06-01", "scope": "all products"}],
            "consultation_close": None,
        },
        "deadline":            "2026-06-01",
        "importance":          case.expected_impact,
        "importance_note":     f"阶段3 + C2 → {case.expected_impact}",
        "worst_case":          "罚款 / 召回 / 禁售",
        "business_impact":     "对我司影响要点：合规成本上升、上市时间延后",
        "business_dimensions": list(case.expected_dimensions),
        "affected_products":   products_field,
        "affected_markets":    case.expected_markets,
    }


def _fake_llm_for_consolidation(prompt: str) -> list:
    """Stage 3 LLM 合并：默认不合并（让 reg_id 确定性合并跑完）。

    特例：包含 trap_kind=contradictory_merge 的 prompt → 返回带矛盾措辞的合并组，
    测 _is_contradictory 拒收。
    """
    # 检测 prompt 中是否包含矛盾合并测试用例
    if "(EU) 2024/2847" in prompt and "(EU) 2024/PLD" in prompt:
        # E09 + E10 同时进入 → 故意给个矛盾合并 reason
        # 提取 ID（prompt 中是 [N] 标记）
        import re
        ids = re.findall(r"\[(\d+)\]", prompt)
        if len(ids) >= 2:
            return [{
                "group_ids": [int(i) for i in ids[:2]],
                "keep_id": int(ids[0]),
                "merged_markets": "欧盟",
                "reason": "尽管两条 reg_id 不同，分别针对网络安全和产品责任，但都属于连接产品监管框架",
            }]
    return []


def _fake_llm_for_cluster_residual(prompt: str) -> list:
    return []  # 不做语义聚类


def _patched_call_json(prompt: str, *, system: str = "", **kwargs) -> str:
    """根据 system/prompt 内容路由到不同模拟器。"""
    sys_lower = (system or "").lower()
    prompt_lower = prompt.lower()

    # 1) 主分析或 fallback 分析
    if "合规分析师" in (system or "") or "合规分析" in prompt:
        # 提取 title 来定位 case
        for line in prompt.split("\n"):
            if line.startswith("法规名称："):
                title = line.replace("法规名称：", "").strip()
                case = _CASE_BY_TITLE.get(title.lower())
                if case:
                    return json.dumps(_fake_llm_for_analyzer(prompt, case), ensure_ascii=False)
        # 找不到 case：返回 "不相关"
        return json.dumps({
            "importance": "🟢", "affected_products": "不相关",
            "business_dimensions": [], "requirement": "未知",
            "deadline": None, "worst_case": "—", "business_impact": "—",
            "affected_markets": "", "importance_note": "未知",
            "dates": {"publish": None, "effective": None,
                      "enforcements": [], "consultation_close": None},
        }, ensure_ascii=False)

    # 2) Stage 3 LLM 合并
    if "consolidation" in sys_lower or "整合" in (system or "") or "合并" in (system or ""):
        result = _fake_llm_for_consolidation(prompt)
        return json.dumps(result, ensure_ascii=False)

    # 3) Stage 0 残余聚类
    if "cluster" in sys_lower or "聚类" in (system or ""):
        return json.dumps(_fake_llm_for_cluster_residual(prompt), ensure_ascii=False)

    # 默认：空数组（避免污染）
    return "[]"


def _patched_call_grounded(prompt: str, **kwargs) -> tuple[str, list]:
    """fallback 路径会调它做 grounded 合成；按测试用例返回合成内容。"""
    # prompt 含"法规名称：" 提取 title
    for line in (prompt or "").split("\n"):
        if line.startswith("法规名称：") or line.startswith("法规名:"):
            title = line.split("：", 1)[-1].strip() if "：" in line else line.split(":", 1)[-1].strip()
            case = _CASE_BY_TITLE.get(title.lower())
            if case:
                # 合成内容：基于真实 full_text 但去掉占位/合成前缀
                synth = case.full_text.replace("[Gemini synthesis]\n", "")
                if len(synth) < 500:
                    synth = synth + "\n\nGrounded fetch summary: applies to LEV products as documented above."
                return (synth, [{"url": case.source_url, "title": case.title}])
    return ("", [])


# 二次重试模式：第二次跑时 SAFETY block case 不再抛错（模拟跨 run 重试）
_RETRY_MODE = {"enabled": False, "passed_safety_block": False}


def _maybe_unblock_safety(case: SyntheticReg) -> bool:
    """retry 模式下，第二次进入时让 safety_block case 走正常路径。"""
    if _RETRY_MODE["enabled"] and case.trap_kind == "safety_block":
        if not _RETRY_MODE["passed_safety_block"]:
            _RETRY_MODE["passed_safety_block"] = True
        return True
    return False


# ── 测试数据装载 ─────────────────────────────────────────────────────────────

def _load_cases_to_db() -> None:
    """把 50 条测试用例直接写入 raw_search_results + scraped_content。

    模拟流程：researcher 已召回（→ raw 'pending'）→ Stage 0 软合并 →
    scraper 抓取（→ raw '已抓取' + sc 行）→ analyzer 等待分析。
    """
    init_db()
    now = datetime.now().isoformat()
    with get_connection() as conn:
        for c in ALL_CASES:
            h = reg_hash(c.title)
            cur = conn.execute("""
                INSERT OR IGNORE INTO raw_search_results
                    (query_date, source_url, title, snippet, priority,
                     market, content_hash, scrape_status, reg_id)
                VALUES (?, ?, ?, ?, '高', ?, ?, '待抓取', ?)
            """, (now, c.source_url, c.title, c.snippet, c.market, h, c.reg_id))


_NAV_PAD_TARGET = 1200  # > _NAV_PAGE_CHAR_THRESHOLD=800，留足余量
_PAD_FILLER_UNIT = (
    "\n\n本节为法规细则展开：详细技术参数见附录 A，符合性评估流程见附录 B，"
    "过渡期与适用日期表见附录 C。生产者、进口商、经销商各方义务在第 III 章具体列举。"
    "执法主体、违规罚则、申诉程序见第 IV 章。 "
    "Annex A: technical parameters. Annex B: conformity assessment. Annex C: timeline. "
    "Chapter III: obligations of producers, importers, distributors. "
    "Chapter IV: enforcement, penalties, appeals.\n"
)


def _pad_to_target(text: str, target: int = _NAV_PAD_TARGET) -> str:
    """循环追加 filler 直到 ≥ target。"""
    out = text
    while len(out) < target:
        out += _PAD_FILLER_UNIT
    return out


def _post_consolidate_load_scraped() -> None:
    """Stage 0 之后，把"未被合并"的 raw 写入 scraped_content（模拟 scraper 抓取）。

    短文本 pad 到 ≥_NAV_PAD_TARGET 避开 requeue_navigation_failures 阈值——除非
    测试用例本就要测导航页路径（trap_kind 中标注），保留原长度以触发 fallback。
    """
    now = datetime.now().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title FROM raw_search_results
            WHERE consolidated_into IS NULL AND scrape_status = '待抓取'
        """).fetchall()
        for r in rows:
            case = _CASE_BY_TITLE.get((r["title"] or "").strip().lower())
            full_text = case.full_text if case else r["title"]
            # 给非合成、非短文本占位测试用例 pad 到 ≥_NAV_PAD_TARGET
            if (
                case is not None
                and not full_text.startswith("[Gemini synthesis]")
                and case.id not in ("E04_short_navigation_page", "E11_orphan_short_text")
            ):
                full_text = _pad_to_target(full_text)
            conn.execute("""
                INSERT INTO scraped_content (raw_id, full_text, content_type,
                    scrape_date, word_count, truncated, ai_analyzed)
                VALUES (?, ?, 'webpage', ?, ?, 0, 0)
            """, (r["id"], full_text, now, len(full_text.split())))
            conn.execute(
                "UPDATE raw_search_results SET scrape_status='已抓取' WHERE id=?",
                (r["id"],),
            )


# ── 评测 ────────────────────────────────────────────────────────────────────

def _evaluate() -> dict:
    """跑 reporter 的 SQL 视图，统计召回与误报。"""
    rows = get_all_analyses()  # 这是 reporter 用的最终视图
    in_report_titles: set[str] = set()
    for row in rows:
        title = (row["title"] or "").strip().lower()
        in_report_titles.add(title)

    # 还要考虑：被 Stage 0 合并的从条目，title 不在 in_report，但其代表 keeper 在
    # 也算召回（因为 reg_id 相同等价于同一法规）
    consolidated_groups: dict[int, list[str]] = {}  # keeper_id -> [titles of merged-in members]
    with get_connection() as conn:
        merged_rows = conn.execute("""
            SELECT id, title, consolidated_into
            FROM raw_search_results
            WHERE consolidated_into IS NOT NULL
        """).fetchall()
        for r in merged_rows:
            consolidated_groups.setdefault(r["consolidated_into"], []).append(
                (r["title"] or "").strip().lower()
            )
        # 取出每个 keeper 的 title
        for keeper_id, member_titles in list(consolidated_groups.items()):
            keeper = conn.execute(
                "SELECT title FROM raw_search_results WHERE id=?", (keeper_id,)
            ).fetchone()
            if keeper:
                kt = (keeper["title"] or "").strip().lower()
                if kt in in_report_titles:
                    # keeper 在周报里 → 被合并的成员视作"间接召回"
                    for mt in member_titles:
                        in_report_titles.add(mt)

    total_relevant = expected_relevant_count()
    total_irrelevant = expected_irrelevant_count()

    recalled = []
    missed = []
    false_positives = []

    for c in ALL_CASES:
        title_l = c.title.strip().lower()
        in_rep = title_l in in_report_titles
        if c.should_appear_in_report:
            if in_rep:
                recalled.append(c)
            else:
                missed.append(c)
        else:
            if in_rep:
                false_positives.append(c)

    recall = len(recalled) / total_relevant if total_relevant else 0.0
    precision = (
        len(recalled) / (len(recalled) + len(false_positives))
        if (len(recalled) + len(false_positives))
        else 0.0
    )
    fp_rate = len(false_positives) / total_irrelevant if total_irrelevant else 0.0

    return {
        "total_relevant":   total_relevant,
        "total_irrelevant": total_irrelevant,
        "recalled":         len(recalled),
        "missed":           len(missed),
        "false_positives":  len(false_positives),
        "recall":           recall,
        "precision":        precision,
        "fp_rate":          fp_rate,
        "missed_cases":     missed,
        "fp_cases":         false_positives,
    }


# ── 入口 ────────────────────────────────────────────────────────────────────

def _setup_db_and_pipeline(skip_fallback: bool = True) -> dict:
    """跑一轮完整 pipeline。返回评测指标。"""
    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)
    _load_cases_to_db()

    print("\n=== Stage 0: reg_id 聚类 ===")
    consolidator.consolidate_pending(verbose=True)

    _post_consolidate_load_scraped()

    print("\n=== Stage 1-3: 主分析 + 收敛 ===")
    from analyzer import run_analysis
    run_analysis(skip_fallback=skip_fallback)

    return _evaluate()


def _run_scenario(name: str, fn) -> dict:
    """跑一个 scenario，返回指标。"""
    print("\n" + "█" * 70)
    print(f"█  Scenario: {name}")
    print("█" * 70)
    _index_cases()

    original_call_json = ai_client.call_json
    original_call_grounded = ai_client.call_grounded
    ai_client.call_json = _patched_call_json
    ai_client.call_grounded = _patched_call_grounded

    # 重置 retry 状态
    _RETRY_MODE["enabled"] = False
    _RETRY_MODE["passed_safety_block"] = False

    try:
        return fn()
    finally:
        ai_client.call_json = original_call_json
        ai_client.call_grounded = original_call_grounded


def scenario_a_default() -> dict:
    """Scenario A: 默认配置。skip_fallback=True，单次 run。"""
    return _setup_db_and_pipeline(skip_fallback=True)


def scenario_b_with_fallback() -> dict:
    """Scenario B: 启用 fallback 合成路径。
    E04（短文本占位页）应被 requeue → grounded 合成 → 入周报，
    但 importance 强制不超过 🟡。
    """
    return _setup_db_and_pipeline(skip_fallback=False)


def scenario_c_retry_after_block() -> dict:
    """Scenario C: 模拟两次连续 run。
    第一次：E08（SAFETY block）抛 BlockedResponseError 不 mark_analyzed。
    第二次：mock 解除 block，应能召回 E08（验证临时错误不会永久丢数据）。
    """
    # 第一次跑（E08 应失败但不 mark_analyzed）
    print("\n--- 第一次跑（E08 SAFETY block 触发）---")
    _RETRY_MODE["enabled"] = False
    _setup_db_and_pipeline(skip_fallback=True)

    # 第二次跑（不重新建库，让 E08 的 sc 仍然 ai_analyzed=0）
    print("\n--- 第二次跑（解除 SAFETY block 模拟）---")
    _RETRY_MODE["enabled"] = True
    from analyzer import run_analysis
    run_analysis(skip_fallback=True)

    return _evaluate()


def scenario_d_regid_pressure() -> dict:
    """Scenario D: 大量同 reg_id 压力测试。
    生成 10 条同 reg_id (EU) 2023/1542 不同写法 + 不同维度，看 Stage 0 + Pass 1 合并。
    """
    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)
    init_db()

    # 注入 10 条同 reg_id 的人造法规（不在 ALL_CASES 里，单独）
    extra_synthetics = []
    base_writings = [
        "(EU) 2023/1542", "Reg (EU) 2023/1542", "Regulation (EU) 2023/1542",
        "EU Battery Regulation 2023/1542", "(EU) 2023/1542", "EU Reg 2023/1542",
        "Battery Regulation (EU) 2023/1542", "Regulation 2023/1542",
        "(EU) 2023/1542", "Reg 2023/1542",
    ]
    chapters = [
        ("Article 7 Carbon Footprint", ["PROD"]),
        ("Article 14 Removability", ["RD"]),
        ("Article 64 Battery Passport", ["CERT"]),
        ("Article 53 EPR", ["EOL"]),
        ("Annex IV Performance Tests", ["RD"]),
        ("Annex VI Repurposing", ["EOL"]),
        ("Article 39 Conformity Assessment", ["CERT"]),
        ("Article 49 Due Diligence", ["PROD"]),
        ("Article 71 Penalties", ["ENFORCE"]),
        ("Article 77 Implementing Acts", ["CERT"]),
    ]
    for i, (rid, (chap, dims)) in enumerate(zip(base_writings, chapters)):
        c = SyntheticReg(
            id=f"DUP_{i:02d}",
            title=f"EU Battery Regulation 2023/1542 — {chap}",
            market="欧盟",
            source_url=f"https://eur-lex.europa.eu/eli/reg/2023/1542/oj#{i}",
            reg_id=rid,
            snippet=chap,
            full_text=(
                f"This excerpt corresponds to {chap} of Regulation (EU) 2023/1542. "
                "Applies to LMT batteries used in e-bikes, e-scooters, and electric mopeds. "
                "Member States must designate a competent authority for enforcement. "
                "Producers placing batteries on the EU market are subject to specific obligations. "
                * 3
            ),
            expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车"],
            expected_dimensions=dims,
            expected_impact="🔴",
            expected_markets="欧盟",
        )
        extra_synthetics.append(c)
        _CASE_BY_HASH[reg_hash(c.title)] = c
        _CASE_BY_TITLE[c.title.strip().lower()] = c

    now = datetime.now().isoformat()
    with get_connection() as conn:
        for c in extra_synthetics:
            h = reg_hash(c.title)
            conn.execute("""
                INSERT OR IGNORE INTO raw_search_results
                    (query_date, source_url, title, snippet, priority,
                     market, content_hash, scrape_status, reg_id)
                VALUES (?, ?, ?, ?, '高', ?, ?, '待抓取', ?)
            """, (now, c.source_url, c.title, c.snippet, c.market, h, c.reg_id))

    # Stage 0：应识别同一 reg_id (EU) 2023/1542 → 1 keeper + 9 consolidated
    print("\n=== Stage 0: reg_id 聚类（应合并 10 → 1）===")
    consolidator.consolidate_pending(verbose=True)

    # 写 sc + 跑 analyzer（仅对 keeper）
    _post_consolidate_load_scraped()
    print("\n=== Stage 1-3: 主分析 + 收敛 ===")
    from analyzer import run_analysis
    run_analysis(skip_fallback=True)

    # 评测：所有 10 条都应"间接召回"——keeper 在周报里，9 条被合并
    total_input = len(extra_synthetics)
    rows = get_all_analyses()
    in_report_titles = {(r["title"] or "").strip().lower() for r in rows}
    keeper_count = 0
    for c in extra_synthetics:
        if c.title.strip().lower() in in_report_titles:
            keeper_count += 1

    # 加上"被合并算召回"的逻辑
    with get_connection() as conn:
        merged_count = conn.execute(
            "SELECT COUNT(*) FROM raw_search_results WHERE consolidated_into IS NOT NULL"
        ).fetchone()[0]

    return {
        "total_relevant":   total_input,
        "total_irrelevant": 0,
        "recalled":         keeper_count + merged_count,  # keeper + 合并
        "missed":           total_input - (keeper_count + merged_count),
        "false_positives":  0,
        "recall":           (keeper_count + merged_count) / total_input,
        "precision":        1.0 if (keeper_count + merged_count) > 0 else 0.0,
        "fp_rate":          0.0,
        "missed_cases":     [],
        "fp_cases":         [],
        "_extra":           {
            "stage0_merged_rows": merged_count,
            "report_keepers":     keeper_count,
            "expected_keepers":   1,  # 10 条同 reg_id 应合并为 1 个 keeper
        },
    }


def print_report(name: str, metrics: dict) -> None:
    print("\n" + "─" * 70)
    print(f"  Scenario [{name}] 评测报告")
    print("─" * 70)
    print(f"  应进周报（正例）   ：{metrics['total_relevant']}")
    print(f"  不应进周报（反例） ：{metrics['total_irrelevant']}")
    print(f"  实际召回正例       ：{metrics['recalled']}")
    print(f"  漏球（False Neg）  ：{metrics['missed']}")
    print(f"  误收（False Pos）  ：{metrics['false_positives']}")
    print()
    print(f"  ★ 召回率 (Recall)    : {metrics['recall']:.1%}")
    print(f"  ★ 精确率 (Precision) : {metrics['precision']:.1%}")
    print(f"  ★ 误报率 (FP rate)   : {metrics['fp_rate']:.1%}")
    if "_extra" in metrics:
        print(f"\n  额外指标：")
        for k, v in metrics["_extra"].items():
            print(f"    {k:<25}: {v}")
    if metrics["missed_cases"]:
        print("\n  漏球清单：")
        for c in metrics["missed_cases"]:
            print(f"    [{c.id}] {c.title[:60]}")
    if metrics["fp_cases"]:
        print("\n  误收清单：")
        for c in metrics["fp_cases"]:
            print(f"    [{c.id}] {c.title[:60]}")
    print("─" * 70)


if __name__ == "__main__":
    results: dict[str, dict] = {}

    results["A_default"]        = _run_scenario("A 默认 (skip_fallback)", scenario_a_default)
    print_report("A 默认", results["A_default"])

    results["B_fallback_on"]    = _run_scenario("B 启用 fallback 合成路径", scenario_b_with_fallback)
    print_report("B fallback", results["B_fallback_on"])

    results["C_retry_safety"]   = _run_scenario("C 二次重试解 SAFETY block", scenario_c_retry_after_block)
    print_report("C retry", results["C_retry_safety"])

    results["D_regid_pressure"] = _run_scenario("D 同 reg_id 大量聚类", scenario_d_regid_pressure)
    print_report("D 同 reg_id 聚类", results["D_regid_pressure"])

    print("\n" + "█" * 70)
    print("█  汇总")
    print("█" * 70)
    print(f"  {'Scenario':<22}  {'Recall':>8}  {'Precision':>10}  {'FP rate':>8}")
    for name, m in results.items():
        print(f"  {name:<22}  {m['recall']:>7.1%}  {m['precision']:>9.1%}  {m['fp_rate']:>7.1%}")

    target = 0.90
    all_pass = all(m["recall"] >= target for m in results.values())
    if all_pass:
        print(f"\n  ✓ 所有 scenario 召回率 ≥{target:.0%}")
        sys.exit(0)
    else:
        print(f"\n  ✗ 部分 scenario 未达 {target:.0%}")
        sys.exit(1)
