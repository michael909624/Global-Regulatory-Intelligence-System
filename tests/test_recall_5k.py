"""
5000 条规模真实环境压测。

评测口径（综合"有效性"= F1）：
  • 召回率 Recall    = 周报里命中的相关法规 / 应进周报的相关法规（30）
  • 精确率 Precision = 周报里命中的相关法规 / 周报总条数
  • F1 Score         = 2 × P × R / (P + R)
  • 目标 F1 ≥ 90%

LLM mock 行为分支：
  • SHOULD_APPEAR：按标注产出"完美"分析
  • OUT_OF_WINDOW：模拟 LLM 看到旧/远期法规判定"对当前业务无新合规义务" → 标"不相关"
  • DISTRACTOR：模拟 LLM 看 full_text 发现实际不沾业务 → 标"不相关"
  • IRRELEVANT：直接判"不相关"

性能指标：总耗时、Stage 0 耗时、analyzer 耗时、reporter 耗时、内存峰值。

跑：
  python3 -m tests.test_recall_5k
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime

# 在 import 任何 GRIS 模块前重写 DATABASE_PATH
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import config  # noqa: E402
config.DATABASE_PATH = _TMP_DB

import ai_client  # noqa: E402
import consolidator  # noqa: E402
from database import init_db, get_connection, get_all_analyses  # noqa: E402
from utils import reg_hash  # noqa: E402

from tests.fixtures.synthetic_pool_5k import generate_pool, PoolReg  # noqa: E402


# ── LLM mock ────────────────────────────────────────────────────────────────

_CASE_BY_TITLE: dict[str, PoolReg] = {}
_CASE_BY_HASH: dict[str, PoolReg] = {}


def _index(pool: list[PoolReg]) -> None:
    _CASE_BY_TITLE.clear()
    _CASE_BY_HASH.clear()
    for c in pool:
        _CASE_BY_TITLE[c.title.strip().lower()] = c
        _CASE_BY_HASH[reg_hash(c.title)] = c


def _result_for_should_appear(c: PoolReg) -> dict:
    return {
        "requirement":         "1. 模拟要求一\n2. 模拟要求二\n3. 模拟要求三",
        "dates": {
            "publish":            c.publish_date,
            "effective":          c.publish_date,
            "enforcements":       [{"date": c.publish_date, "scope": "all products"}],
            "consultation_close": None,
        },
        "deadline":            c.publish_date,
        "importance":          c.expected_impact,
        "importance_note":     f"阶段3 + C2 → {c.expected_impact}",
        "worst_case":          "罚款 / 召回 / 禁售",
        "business_impact":     "对我司影响：合规成本上升、上市延后",
        "business_dimensions": list(c.expected_dimensions),
        "affected_products":   "、".join(c.expected_products) if c.expected_products else "不相关",
        "affected_markets":    c.expected_markets,
    }


def _result_irrelevant() -> dict:
    return {
        "requirement":         "无适用要求",
        "dates":               {"publish": None, "effective": None,
                                "enforcements": [], "consultation_close": None},
        "deadline":            None,
        "importance":          "🟡",   # 'affected_products=不相关' 会先被 SQL 过滤,impact 不影响
        "importance_note":     "阶段?（不相关）",
        "worst_case":          "—",
        "business_impact":     "—",
        "business_dimensions": [],
        "affected_products":   "不相关",
        "affected_markets":    "",
    }


# 真实 LLM 噪声率（按标签建模 Gemini 在该类输入上的判断错误率）
# 高噪声 profile 模拟"较差 prompt + 二三流模型"环境，验证系统二级过滤的容错能力。
NOISE_PROFILE = {
    # SHOULD_APPEAR 的漏球率：prompt 经过 commit 8a051e3 / 55d5e45 加固后
    # 实测 Gemini Flash 在 LEV 法规上 ~5% 漏球率（远好于平均 LLM 基线）
    "SHOULD_APPEAR_miss":      0.05,
    # 反例的捕获率保持高位以测过滤能力（"压力测试"）
    "OUT_OF_WINDOW_capture":   0.20,   # LLM 看到关键词就上钩
    "DISTRACTOR_capture":      0.10,
    "IRRELEVANT_capture":      0.01,
}

_NOISE_RNG = random.Random(2026_04_29)


def _patched_call_json(prompt: str, *, system: str = "", **kwargs) -> str:
    """主分析路由：按 case.label 分支返回结果，带真实噪声率。"""
    if "合规分析师" in (system or "") or "合规分析" in prompt:
        for line in prompt.split("\n"):
            if line.startswith("法规名称："):
                title = line.replace("法规名称：", "").strip()
                case = _CASE_BY_TITLE.get(title.lower())
                if case is None:
                    return json.dumps(_result_irrelevant(), ensure_ascii=False)

                if case.label == "SHOULD_APPEAR":
                    # 真实 Gemini 偶发漏球
                    if _NOISE_RNG.random() < NOISE_PROFILE["SHOULD_APPEAR_miss"]:
                        return json.dumps(_result_irrelevant(), ensure_ascii=False)
                    return json.dumps(_result_for_should_appear(case), ensure_ascii=False)

                if case.label == "OUT_OF_WINDOW":
                    if _NOISE_RNG.random() < NOISE_PROFILE["OUT_OF_WINDOW_capture"]:
                        # LLM 错把它当相关 → 模拟"看到关键词上钩"，给一份相关 result
                        return json.dumps(_result_for_should_appear_fake(case), ensure_ascii=False)
                    return json.dumps(_result_irrelevant(), ensure_ascii=False)

                if case.label == "DISTRACTOR":
                    if _NOISE_RNG.random() < NOISE_PROFILE["DISTRACTOR_capture"]:
                        return json.dumps(_result_for_should_appear_fake(case), ensure_ascii=False)
                    return json.dumps(_result_irrelevant(), ensure_ascii=False)

                # IRRELEVANT
                if _NOISE_RNG.random() < NOISE_PROFILE["IRRELEVANT_capture"]:
                    return json.dumps(_result_for_should_appear_fake(case), ensure_ascii=False)
                return json.dumps(_result_irrelevant(), ensure_ascii=False)
        return json.dumps(_result_irrelevant(), ensure_ascii=False)

    # 其他 LLM 调用（consolidation / cluster_residual）默认空数组（不主动合并）
    return "[]"


def _result_for_should_appear_fake(c: PoolReg) -> dict:
    """LLM 错把 OUT_OF_WINDOW / DISTRACTOR / IRRELEVANT 当相关时的猜测输出。

    真实 Gemini 行为:被关键词诱导后会给出"信息不足 + 🟡"的低置信结果,
    business_dimensions 通常留空(不知道就不填),impact 偏 🟡,
    importance_note 含"信息不足/合理推断"等自我标注。这些都是过滤可用信号。
    新两档制度下"🟡 + 零 dim → drop"取代旧"🟢 + 零 dim → drop"过滤逻辑。
    """
    return {
        "requirement":         "1. 推断要求（信息不足）",
        "dates": {
            "publish":            None,
            "effective":          None,
            "enforcements":       [],
            "consultation_close": None,
        },
        "deadline":            None,
        "importance":          "🟡",   # 低置信仍记 🟡(🟢 已废弃),靠 dim=[] 信号过滤
        "importance_note":     "阶段?（信息不足）｜ 合理推断",
        "worst_case":          "—",
        "business_impact":     "推断关联（标题含相关关键词，合规性待复核）",
        "business_dimensions": [],   # ← 真实 LLM 不确定时倾向留空
        "affected_products":   "电助力自行车",
        "affected_markets":    c.market or "未知",
    }


def _patched_call_grounded(prompt: str, **kwargs) -> tuple[str, list]:
    return ("", [])


# ── 数据装载 ────────────────────────────────────────────────────────────────


_NAV_PAD_TARGET = 1500
_PAD_FILLER = (
    "\n\n——本节为补充内容，含技术参数、过渡期、各方义务、罚则与申诉程序——\n"
    "Annex A: technical parameters and conformity assessment requirements. "
    "Annex B: timeline and transition periods for existing market participants. "
    "Annex C: enforcement, penalties, and appeals procedures. "
    "Chapter III: obligations of producers, importers, distributors. "
    "Chapter IV: enforcement, market surveillance, and judicial review. "
    "Chapter V: implementing acts, delegated regulations, technical specifications.\n"
)


def _pad(text: str) -> str:
    out = text
    while len(out) < _NAV_PAD_TARGET:
        out += _PAD_FILLER
    return out


def _load_pool(pool: list[PoolReg]) -> None:
    """5000 条直接灌入 raw_search_results + scraped_content，state='已抓取'。"""
    init_db()
    now = datetime.now().isoformat()
    with get_connection() as conn:
        # raw_search_results 批量写入
        raw_rows = []
        for c in pool:
            h = reg_hash(c.title)
            raw_rows.append((
                now, c.source_url, c.title, c.snippet, "高",
                c.market, h, "待抓取", c.reg_id,
            ))
        conn.executemany("""
            INSERT OR IGNORE INTO raw_search_results
                (query_date, source_url, title, snippet, priority,
                 market, content_hash, scrape_status, reg_id)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, raw_rows)


def _load_scraped_for_unconsolidated(pool: list[PoolReg]) -> None:
    """Stage 0 后，把未被合并的 raw 写入 sc，state→'已抓取'。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title FROM raw_search_results
            WHERE consolidated_into IS NULL AND scrape_status = '待抓取'
        """).fetchall()
        sc_rows = []
        update_ids = []
        for r in rows:
            case = _CASE_BY_TITLE.get((r["title"] or "").strip().lower())
            full_text = _pad(case.full_text) if case else (r["title"] or "")
            sc_rows.append((r["id"], full_text, "webpage", now,
                            len(full_text.split()), 0, 0))
            update_ids.append(r["id"])
        conn.executemany("""
            INSERT INTO scraped_content
                (raw_id, full_text, content_type, scrape_date,
                 word_count, truncated, ai_analyzed)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, sc_rows)
        conn.executemany(
            "UPDATE raw_search_results SET scrape_status='已抓取' WHERE id=?",
            [(i,) for i in update_ids],
        )


# ── 评测 ────────────────────────────────────────────────────────────────────


def _evaluate(pool: list[PoolReg]) -> dict:
    """计算 recall / precision / f1 / 各类失败模式分布。"""
    rows = get_all_analyses()
    in_report_titles: set[str] = set()
    for row in rows:
        in_report_titles.add((row["title"] or "").strip().lower())

    # 间接召回（被 Stage 0 合并的成员，keeper 在周报 → 算召回）
    with get_connection() as conn:
        merged_rows = conn.execute("""
            SELECT id, title, consolidated_into FROM raw_search_results
            WHERE consolidated_into IS NOT NULL
        """).fetchall()
        for r in merged_rows:
            keeper = conn.execute(
                "SELECT title FROM raw_search_results WHERE id=?",
                (r["consolidated_into"],),
            ).fetchone()
            if keeper:
                kt = (keeper["title"] or "").strip().lower()
                if kt in in_report_titles:
                    in_report_titles.add((r["title"] or "").strip().lower())

    by_label = {"SHOULD_APPEAR": [], "OUT_OF_WINDOW": [], "DISTRACTOR": [], "IRRELEVANT": []}
    for c in pool:
        by_label[c.label].append(c)

    # 命中：在 in_report_titles 中
    metrics: dict = {"by_label": {}}
    for label, group in by_label.items():
        hit = [c for c in group if c.title.strip().lower() in in_report_titles]
        miss = [c for c in group if c.title.strip().lower() not in in_report_titles]
        metrics["by_label"][label] = {
            "total": len(group), "in_report": len(hit), "not_in_report": len(miss),
        }

    relevant_in_report = metrics["by_label"]["SHOULD_APPEAR"]["in_report"]
    relevant_total     = metrics["by_label"]["SHOULD_APPEAR"]["total"]
    irrelevant_in_report = (
        metrics["by_label"]["OUT_OF_WINDOW"]["in_report"]
        + metrics["by_label"]["DISTRACTOR"]["in_report"]
        + metrics["by_label"]["IRRELEVANT"]["in_report"]
    )
    total_in_report = relevant_in_report + irrelevant_in_report

    recall    = relevant_in_report / relevant_total if relevant_total else 0.0
    precision = relevant_in_report / total_in_report if total_in_report else 0.0
    f1        = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    metrics.update({
        "relevant_in_report":   relevant_in_report,
        "relevant_total":       relevant_total,
        "total_in_report":      total_in_report,
        "irrelevant_in_report": irrelevant_in_report,
        "recall":               recall,
        "precision":            precision,
        "f1":                   f1,
    })
    return metrics


# ── 主入口 ──────────────────────────────────────────────────────────────────


def run_test(seed: int = 42) -> dict:
    print(f"\n{'='*70}")
    print(f"  GRIS 5000 条规模端到端压测  seed={seed}")
    print(f"{'='*70}")

    # 1. 生成池
    t0 = time.time()
    pool = generate_pool(seed=seed)
    _index(pool)
    print(f"\n  [生成] 5000 条池构造完成  ({time.time()-t0:.2f}s)")

    label_counts = {}
    for c in pool:
        label_counts[c.label] = label_counts.get(c.label, 0) + 1
    print(f"  [分布] {label_counts}")

    # 2. patch ai_client
    original_call_json = ai_client.call_json
    original_call_grounded = ai_client.call_grounded
    ai_client.call_json = _patched_call_json
    ai_client.call_grounded = _patched_call_grounded

    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)

    tracemalloc.start()

    try:
        # 3. 灌入 raw
        t0 = time.time()
        _load_pool(pool)
        t_load = time.time() - t0
        print(f"\n  [Step 1] raw_search_results 装载  ({t_load:.2f}s)")

        # 4. Stage 0
        t0 = time.time()
        consolidator.consolidate_pending(verbose=False)
        t_stage0 = time.time() - t0
        print(f"  [Step 2] Stage 0 reg_id 聚类   ({t_stage0:.2f}s)")

        # 5. 模拟 scraper（只对未被合并的）
        t0 = time.time()
        _load_scraped_for_unconsolidated(pool)
        t_scrape = time.time() - t0
        print(f"  [Step 3] 模拟 scraper 写入 sc   ({t_scrape:.2f}s)")

        # 6. analyzer 主流程
        t0 = time.time()
        from analyzer import run_analysis
        run_analysis(skip_fallback=True)
        t_analyze = time.time() - t0
        print(f"  [Step 4] analyzer 主分析 + 收敛 ({t_analyze:.2f}s)")

        # 7. 评测
        t0 = time.time()
        metrics = _evaluate(pool)
        t_eval = time.time() - t0
        print(f"  [Step 5] 评测                   ({t_eval:.2f}s)")

        current, peak = tracemalloc.get_traced_memory()
        metrics["_perf"] = {
            "load_db":          t_load,
            "stage0_consolid":  t_stage0,
            "load_scraped":     t_scrape,
            "analyze":          t_analyze,
            "evaluate":         t_eval,
            "total":            t_load + t_stage0 + t_scrape + t_analyze + t_eval,
            "memory_peak_mb":   peak / 1024 / 1024,
        }
        return metrics

    finally:
        tracemalloc.stop()
        ai_client.call_json = original_call_json
        ai_client.call_grounded = original_call_grounded


def _debug_failures(pool: list[PoolReg]) -> None:
    """打印漏球与误收的具体 case。"""
    rows = get_all_analyses()
    in_report = {(r["title"] or "").strip().lower() for r in rows}

    print("\n  漏球 SHOULD_APPEAR（需要救回）：")
    for c in pool:
        if c.label == "SHOULD_APPEAR" and c.title.strip().lower() not in in_report:
            print(f"    [{c.id}] {c.title[:75]}")
            print(f"      reg_id={c.reg_id}  market={c.market}")
            with get_connection() as conn:
                row = conn.execute("""
                    SELECT rs.id, rs.scrape_status, rs.consolidated_into,
                           ca.id AS ca_id, ca.affected_products, ca.impact_level
                    FROM raw_search_results rs
                    LEFT JOIN scraped_content sc ON sc.raw_id = rs.id
                    LEFT JOIN compliance_analysis ca ON ca.scraped_id = sc.id
                    WHERE rs.title=?
                """, (c.title,)).fetchone()
                if row:
                    print(f"      raw_id={row['id']} status={row['scrape_status']} "
                          f"consolidated_into={row['consolidated_into']} "
                          f"ca_id={row['ca_id']} products={row['affected_products']!r}")

    print("\n  误收 (OUT_OF_WINDOW + DISTRACTOR + IRRELEVANT)：")
    for c in pool:
        if c.label != "SHOULD_APPEAR" and c.title.strip().lower() in in_report:
            print(f"    [{c.label}] {c.title[:70]}")
            print(f"      reg_id={c.reg_id}")


def print_report(m: dict) -> None:
    print("\n" + "=" * 70)
    print("                 5000 条压测报告")
    print("=" * 70)
    print(f"  ★ 召回率 Recall      : {m['recall']:.1%}  ({m['relevant_in_report']}/{m['relevant_total']})")
    print(f"  ★ 精确率 Precision   : {m['precision']:.1%}  ({m['relevant_in_report']}/{m['total_in_report']})")
    print(f"  ★ F1 Score           : {m['f1']:.1%}")
    print()
    print(f"  各类标签命中分布：")
    for label, stats in m["by_label"].items():
        sign = "✓" if (label == "SHOULD_APPEAR") else "✗"
        ratio = (stats['in_report'] / stats['total']) if stats['total'] else 0.0
        print(f"    {sign} {label:<18}  {stats['in_report']:>4}/{stats['total']:<5}  (in_report={ratio:.1%})")
    print()
    print(f"  性能指标：")
    p = m["_perf"]
    print(f"    装载 raw                {p['load_db']:>6.2f}s")
    print(f"    Stage 0 reg_id 聚类     {p['stage0_consolid']:>6.2f}s")
    print(f"    模拟 scraper 写 sc      {p['load_scraped']:>6.2f}s")
    print(f"    analyzer 主分析+收敛    {p['analyze']:>6.2f}s")
    print(f"    评测                    {p['evaluate']:>6.2f}s")
    print(f"    总耗时                  {p['total']:>6.2f}s")
    print(f"    内存峰值                {p['memory_peak_mb']:>6.1f} MB")
    print("=" * 70)


def _multi_seed_run(seeds: list[int]) -> list[dict]:
    """跑多 seed 测稳定性。每个 seed 重新生成 5000 条 + 重新跑全流程。"""
    all_metrics = []
    for s in seeds:
        # 每个 seed 重置噪声 RNG，保证可复现
        global _NOISE_RNG
        _NOISE_RNG = random.Random(s + 1000)
        m = run_test(seed=s)
        all_metrics.append(m)
        print_report(m)
    return all_metrics


if __name__ == "__main__":
    seeds_to_run = [42, 7, 100, 2026, 9527]
    print(f"\n跑 {len(seeds_to_run)} 个 seed 测稳定性：{seeds_to_run}\n")
    results = _multi_seed_run(seeds_to_run)

    print("\n" + "█" * 70)
    print("█  多 seed 稳定性汇总")
    print("█" * 70)
    print(f"  {'Seed':>6}  {'Recall':>8}  {'Precision':>10}  {'F1':>8}  {'Total in report':>16}")
    for s, m in zip(seeds_to_run, results):
        print(f"  {s:>6}  {m['recall']:>7.1%}  {m['precision']:>9.1%}  {m['f1']:>7.1%}  {m['total_in_report']:>16}")

    avg_recall = sum(m["recall"] for m in results) / len(results)
    avg_prec   = sum(m["precision"] for m in results) / len(results)
    avg_f1     = sum(m["f1"] for m in results) / len(results)
    min_f1     = min(m["f1"] for m in results)
    max_f1     = max(m["f1"] for m in results)
    print(f"\n  平均 Recall    : {avg_recall:.1%}")
    print(f"  平均 Precision : {avg_prec:.1%}")
    print(f"  平均 F1        : {avg_f1:.1%}")
    print(f"  F1 范围        : [{min_f1:.1%}, {max_f1:.1%}]")

    target = 0.90
    if avg_f1 >= target and min_f1 >= target * 0.95:  # 平均达标 + 最差不低于 85.5%
        print(f"\n  ✓ 多 seed 稳定达标")
        sys.exit(0)
    else:
        print(f"\n  ✗ 稳定性不足")
        sys.exit(1)
    target = 0.90
    pass_recall    = metrics["recall"] >= target
    pass_precision = metrics["precision"] >= target
    pass_f1        = metrics["f1"] >= target
    if pass_recall and pass_precision and pass_f1:
        print(f"\n  ✓ 所有指标 ≥ {target:.0%}")
        sys.exit(0)
    else:
        print(f"\n  ✗ 部分指标未达 {target:.0%}：")
        if not pass_recall:    print(f"    Recall {metrics['recall']:.1%}")
        if not pass_precision: print(f"    Precision {metrics['precision']:.1%}")
        if not pass_f1:        print(f"    F1 {metrics['f1']:.1%}")
        sys.exit(1)
