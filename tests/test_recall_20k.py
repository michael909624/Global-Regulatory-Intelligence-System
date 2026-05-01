"""
20000 条规模端到端召回率测试（含 7 类失败模式）。

评测口径：
  应抓回 = SHOULD_APPEAR + BAD_URL_RELEVANT (= 150 条)
  实际抓回 = 进周报且 title 是上述两类的命中数
  召回率 Recall    = 实际抓回 / 应抓回           （目标 ≥ 90%）
  精确率 Precision = 命中 / 周报总条数
  F1 Score         = 2PR / (P+R)

7 类标签：
  • SHOULD_APPEAR        100  正常 sc + 应进周报
  • BAD_URL_RELEVANT      50  scrape_status='失败' → fallback grounded 救回
  • OUT_OF_WINDOW       1500  时间窗外的相关法规 → LLM 判"无新合规"
  • DISTRACTOR          2500  标题像但内容不沾
  • TITLE_URL_MISMATCH   250  title 像合规但 sc 是无关短段落 → 应被 requeue + fallback 拦
  • BAD_URL_404          100  URL 失效 + 占位编号 → fallback 启发式过滤
  • IRRELEVANT         15500  完全不相关

不真调 Gemini，全程 mock。模拟真实 Gemini 噪声率（基于 5K 测试观察）。

跑：python3 -m tests.test_recall_20k [--debug]
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

from tests.fixtures.synthetic_pool_20k import generate_pool_20k  # noqa: E402
from tests.fixtures.synthetic_pool_5k import PoolReg  # noqa: E402


# ── 索引 ────────────────────────────────────────────────────────────────────


_CASE_BY_TITLE: dict[str, PoolReg] = {}


def _index(pool: list[PoolReg]) -> None:
    _CASE_BY_TITLE.clear()
    for c in pool:
        _CASE_BY_TITLE[c.title.strip().lower()] = c


# ── LLM mock ────────────────────────────────────────────────────────────────


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
        "importance":          "🟡",   # 'affected_products=不相关' 先 SQL 过滤,impact 不影响
        "importance_note":     "阶段?（不相关）",
        "worst_case":          "—",
        "business_impact":     "—",
        "business_dimensions": [],
        "affected_products":   "不相关",
        "affected_markets":    "",
    }


def _result_low_confidence_guess(c: PoolReg) -> dict:
    """LLM 被关键词诱导 + 信息不足时的猜测输出(dims=[] + 🟡,应被周报视图过滤掉)。
    新两档制度下"🟡 + 零 dim → drop"取代旧"🟢 + 零 dim → drop"(commit 1ed7b32)。"""
    return {
        "requirement":         "1. 推断要求（信息不足）",
        "dates":               {"publish": None, "effective": None,
                                "enforcements": [], "consultation_close": None},
        "deadline":            None,
        "importance":          "🟡",   # 低置信仍记 🟡(🟢 已废弃),靠 dim=[] 信号过滤
        "importance_note":     "阶段?（信息不足）｜ 合理推断",
        "worst_case":          "—",
        "business_impact":     "推断关联（标题含相关关键词，合规性待复核）",
        "business_dimensions": [],
        "affected_products":   "电助力自行车",
        "affected_markets":    c.market or "未知",
    }


# 真实 Gemini 噪声率（按 5K 测试观察的分类错误率建模）
NOISE_PROFILE = {
    "SHOULD_APPEAR_miss":      0.05,   # 真相关漏球
    "OUT_OF_WINDOW_capture":   0.20,   # LLM 看关键词上钩，但应被低置信过滤拦下
    "DISTRACTOR_capture":      0.10,
    "IRRELEVANT_capture":      0.01,
    "TITLE_MISMATCH_capture":  0.30,   # title 像 + 内容是导航页时 LLM 易上钩
}

_NOISE_RNG = random.Random(0)


def _patched_call_json(prompt: str, *, system: str = "", **kwargs) -> str:
    """主分析 / fallback 分析路由：按 case.label 分支，模拟真实 LLM 噪声。"""
    if "合规分析师" not in (system or "") and "合规分析" not in prompt:
        # 其它 LLM 调用（cluster / consolidation）默认不主动合并
        return "[]"

    title = ""
    for line in prompt.split("\n"):
        if line.startswith("法规名称："):
            title = line.replace("法规名称：", "").strip()
            break

    case = _CASE_BY_TITLE.get(title.lower())
    if case is None:
        return json.dumps(_result_irrelevant(), ensure_ascii=False)

    # SHOULD_APPEAR + BAD_URL_RELEVANT 都标 label="SHOULD_APPEAR"
    if case.label == "SHOULD_APPEAR":
        if _NOISE_RNG.random() < NOISE_PROFILE["SHOULD_APPEAR_miss"]:
            return json.dumps(_result_irrelevant(), ensure_ascii=False)
        return json.dumps(_result_for_should_appear(case), ensure_ascii=False)

    if case.label == "OUT_OF_WINDOW":
        if _NOISE_RNG.random() < NOISE_PROFILE["OUT_OF_WINDOW_capture"]:
            return json.dumps(_result_low_confidence_guess(case), ensure_ascii=False)
        return json.dumps(_result_irrelevant(), ensure_ascii=False)

    if case.label == "DISTRACTOR":
        if _NOISE_RNG.random() < NOISE_PROFILE["DISTRACTOR_capture"]:
            return json.dumps(_result_low_confidence_guess(case), ensure_ascii=False)
        return json.dumps(_result_irrelevant(), ensure_ascii=False)

    # IRRELEVANT 包含三个子类：normal、TITLE_URL_MISMATCH、BAD_URL_404
    if case.scrape_outcome == "mismatch":
        # title 像合规——LLM 容易被关键词诱导上钩
        if _NOISE_RNG.random() < NOISE_PROFILE["TITLE_MISMATCH_capture"]:
            return json.dumps(_result_low_confidence_guess(case), ensure_ascii=False)
        return json.dumps(_result_irrelevant(), ensure_ascii=False)

    # 普通 IRRELEVANT
    if _NOISE_RNG.random() < NOISE_PROFILE["IRRELEVANT_capture"]:
        return json.dumps(_result_low_confidence_guess(case), ensure_ascii=False)
    return json.dumps(_result_irrelevant(), ensure_ascii=False)


def _patched_call_grounded(prompt: str, **kwargs) -> tuple[str, list]:
    """fallback grounded fetch mock：
       BAD_URL_RELEVANT (label=SHOULD_APPEAR + outcome=fail) → 返回合成内容
       BAD_URL_404 + TITLE_URL_MISMATCH → 返回空（让 fallback 标"需人工"）
    """
    title = ""
    for line in prompt.split("\n"):
        if line.startswith("法规名称："):
            title = line.replace("法规名称：", "").strip()
            break

    case = _CASE_BY_TITLE.get(title.lower())
    if case is None:
        return ("", [])

    if case.label == "SHOULD_APPEAR" and case.scrape_outcome == "fail":
        # 模拟 grounded fetch 拼到了相关内容
        synth_text = (
            f"This regulation ({case.reg_id}) applies to {case.market}. "
            f"Effective: {case.publish_date}. Reference: {case.reg_id}. "
            f"Article 1 - Scope: applies to manufacturers, importers and distributors. "
            f"Article 2 - Conformity assessment by notified body. "
            f"Article 3 - Effective date and transitional period. "
            f"Article 4 - Penalties up to 4% of turnover. "
            f"Annex A: technical parameters. Annex B: marking and labeling. "
            f"Annex C: enforcement and appeals."
        ) * 4
        return (synth_text, [])

    # 其它情况：grounded 找不到——返回空
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
    """按 scrape_outcome 分流写入：
       ok       → raw + 待抓取（后续 _load_scraped 写 sc，状态'已抓取'）
       fail     → raw + 直接 scrape_status='失败'（不写 sc，等 fallback）
       mismatch → raw + 待抓取（后续 _load_scraped 写 sc 但是短无关文本）
    """
    init_db()
    now = datetime.now().isoformat()
    with get_connection() as conn:
        raw_rows = []
        for c in pool:
            h = reg_hash(c.title)
            initial_status = "失败" if c.scrape_outcome == "fail" else "待抓取"
            raw_rows.append((
                now, c.source_url, c.title, c.snippet, "高",
                c.market, h, initial_status, c.reg_id,
            ))
        conn.executemany("""
            INSERT OR IGNORE INTO raw_search_results
                (query_date, source_url, title, snippet, priority,
                 market, content_hash, scrape_status, reg_id)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, raw_rows)


def _load_scraped(pool: list[PoolReg]) -> None:
    """模拟 scraper 阶段：对 outcome='ok' 写完整 sc；对 outcome='mismatch' 写短无关 sc。"""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title FROM raw_search_results
            WHERE consolidated_into IS NULL
              AND scrape_status = '待抓取'
        """).fetchall()
        sc_rows = []
        update_ids = []
        for r in rows:
            case = _CASE_BY_TITLE.get((r["title"] or "").strip().lower())
            if case is None:
                # _dedupe_titles 加了序号 → fallback：用 title 当 full_text
                full_text = r["title"] or ""
            elif case.scrape_outcome == "mismatch":
                full_text = case.full_text   # 短无关段落原样写入
            else:
                full_text = _pad(case.full_text)
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
    """目标分母 = SHOULD_APPEAR 标签的全体（含 BAD_URL_RELEVANT）= 150 条。"""
    rows = get_all_analyses()
    in_report_titles: set[str] = set()
    for row in rows:
        in_report_titles.add((row["title"] or "").strip().lower())

    # 间接召回：被 Stage 0 合并的成员，keeper 在周报 → 算召回
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

    # 按 (label, scrape_outcome) 分组细分统计
    sub_groups: dict[str, list[PoolReg]] = {
        "SHOULD_APPEAR_normal":     [],
        "BAD_URL_RELEVANT":         [],
        "OUT_OF_WINDOW":            [],
        "DISTRACTOR":               [],
        "TITLE_URL_MISMATCH":       [],
        "BAD_URL_404":              [],
        "IRRELEVANT":               [],
    }
    for c in pool:
        if c.label == "SHOULD_APPEAR" and c.scrape_outcome == "fail":
            sub_groups["BAD_URL_RELEVANT"].append(c)
        elif c.label == "SHOULD_APPEAR":
            sub_groups["SHOULD_APPEAR_normal"].append(c)
        elif c.label == "OUT_OF_WINDOW":
            sub_groups["OUT_OF_WINDOW"].append(c)
        elif c.label == "DISTRACTOR":
            sub_groups["DISTRACTOR"].append(c)
        elif c.scrape_outcome == "mismatch":
            sub_groups["TITLE_URL_MISMATCH"].append(c)
        elif c.scrape_outcome == "fail":
            sub_groups["BAD_URL_404"].append(c)
        else:
            sub_groups["IRRELEVANT"].append(c)

    metrics = {"by_subgroup": {}}
    for sub, group in sub_groups.items():
        hit = [c for c in group if c.title.strip().lower() in in_report_titles]
        metrics["by_subgroup"][sub] = {
            "total": len(group),
            "in_report": len(hit),
            "ratio": (len(hit) / len(group)) if group else 0.0,
        }

    relevant_in_report = (
        metrics["by_subgroup"]["SHOULD_APPEAR_normal"]["in_report"]
        + metrics["by_subgroup"]["BAD_URL_RELEVANT"]["in_report"]
    )
    relevant_total = (
        metrics["by_subgroup"]["SHOULD_APPEAR_normal"]["total"]
        + metrics["by_subgroup"]["BAD_URL_RELEVANT"]["total"]
    )
    irrelevant_in_report = (
        metrics["by_subgroup"]["OUT_OF_WINDOW"]["in_report"]
        + metrics["by_subgroup"]["DISTRACTOR"]["in_report"]
        + metrics["by_subgroup"]["TITLE_URL_MISMATCH"]["in_report"]
        + metrics["by_subgroup"]["BAD_URL_404"]["in_report"]
        + metrics["by_subgroup"]["IRRELEVANT"]["in_report"]
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


def run_test(seed: int = 42, debug: bool = False) -> dict:
    print(f"\n{'='*72}")
    print(f"  GRIS 20000 条规模端到端召回率测试  seed={seed}")
    print(f"{'='*72}")

    t0 = time.time()
    pool = generate_pool_20k(seed=seed)
    _index(pool)
    print(f"\n  [生成] 20000 条池子构造完成  ({time.time()-t0:.2f}s)")

    from collections import Counter
    label_counts = Counter(p.label for p in pool)
    outcome_counts = Counter(p.scrape_outcome for p in pool)
    print(f"  [分布 label]   {dict(label_counts)}")
    print(f"  [分布 outcome] {dict(outcome_counts)}")

    # patch ai_client
    original_call_json = ai_client.call_json
    original_call_grounded = ai_client.call_grounded
    ai_client.call_json = _patched_call_json
    ai_client.call_grounded = _patched_call_grounded

    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)

    tracemalloc.start()

    try:
        t0 = time.time()
        _load_pool(pool)
        t_load = time.time() - t0
        print(f"\n  [Step 1] raw 装载                     ({t_load:.2f}s)")

        t0 = time.time()
        consolidator.consolidate_pending(verbose=False)
        t_stage0 = time.time() - t0
        print(f"  [Step 2] Stage 0 reg_id 聚类          ({t_stage0:.2f}s)")

        t0 = time.time()
        _load_scraped(pool)
        t_scrape = time.time() - t0
        print(f"  [Step 3] 模拟 scraper 写 sc           ({t_scrape:.2f}s)")

        # 启用 fallback——验证 BAD_URL_RELEVANT 能被救回
        t0 = time.time()
        from analyzer import run_analysis
        run_analysis(skip_fallback=False)
        t_analyze = time.time() - t0
        print(f"  [Step 4] analyzer 主分析+fallback+收敛 ({t_analyze:.2f}s)")

        t0 = time.time()
        metrics = _evaluate(pool)
        t_eval = time.time() - t0
        print(f"  [Step 5] 评测                         ({t_eval:.2f}s)")

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

        if debug:
            _debug_failures(pool)

        return metrics
    finally:
        tracemalloc.stop()
        ai_client.call_json = original_call_json
        ai_client.call_grounded = original_call_grounded


def _debug_failures(pool: list[PoolReg]) -> None:
    """打印漏球与误收的具体 case。"""
    rows = get_all_analyses()
    in_report = {(r["title"] or "").strip().lower() for r in rows}

    print("\n  漏球 SHOULD_APPEAR (含 BAD_URL_RELEVANT)：")
    miss_count = 0
    for c in pool:
        if c.label == "SHOULD_APPEAR" and c.title.strip().lower() not in in_report:
            miss_count += 1
            tag = f"[{c.scrape_outcome}]" if c.scrape_outcome != "ok" else "    "
            print(f"    {tag} {c.title[:80]}")
            with get_connection() as conn:
                row = conn.execute("""
                    SELECT rs.id, rs.scrape_status, rs.consolidated_into,
                           ca.id AS ca_id, ca.affected_products, ca.impact_level,
                           ca.business_dimensions
                    FROM raw_search_results rs
                    LEFT JOIN scraped_content sc ON sc.raw_id = rs.id
                    LEFT JOIN compliance_analysis ca ON ca.scraped_id = sc.id
                    WHERE rs.title=?
                """, (c.title,)).fetchone()
                if row:
                    print(f"      raw_id={row['id']} status={row['scrape_status']} "
                          f"into={row['consolidated_into']} ca_id={row['ca_id']} "
                          f"prod={row['affected_products']!r} dim={row['business_dimensions']}")
            if miss_count >= 30:
                print("    ... (truncated)")
                break

    print("\n  误收 (非 SHOULD_APPEAR 进入周报)：")
    capt = 0
    for c in pool:
        if c.label != "SHOULD_APPEAR" and c.title.strip().lower() in in_report:
            capt += 1
            sub = c.scrape_outcome
            print(f"    [{c.label}/{sub}] {c.title[:75]}")
            if capt >= 30:
                print("    ... (truncated)")
                break


def print_report(m: dict) -> None:
    print("\n" + "=" * 72)
    print("                       20000 条召回率报告")
    print("=" * 72)
    print(f"  ★ 召回率 Recall      : {m['recall']:.1%}  ({m['relevant_in_report']}/{m['relevant_total']})")
    print(f"  ★ 精确率 Precision   : {m['precision']:.1%}  ({m['relevant_in_report']}/{m['total_in_report']})")
    print(f"  ★ F1 Score           : {m['f1']:.1%}")
    print()
    print(f"  各子类命中分布：")
    for sub, stats in m["by_subgroup"].items():
        sign = "✓" if sub in ("SHOULD_APPEAR_normal", "BAD_URL_RELEVANT") else "✗"
        print(f"    {sign} {sub:<22}  {stats['in_report']:>5}/{stats['total']:<6}  ({stats['ratio']:.1%})")
    print()
    print(f"  性能指标：")
    p = m["_perf"]
    print(f"    装载 raw                 {p['load_db']:>6.2f}s")
    print(f"    Stage 0 reg_id 聚类      {p['stage0_consolid']:>6.2f}s")
    print(f"    模拟 scraper 写 sc       {p['load_scraped']:>6.2f}s")
    print(f"    主分析+fallback+收敛     {p['analyze']:>6.2f}s")
    print(f"    评测                     {p['evaluate']:>6.2f}s")
    print(f"    总耗时                   {p['total']:>6.2f}s")
    print(f"    内存峰值                 {p['memory_peak_mb']:>6.1f} MB")
    print("=" * 72)


def _multi_seed_run(seeds: list[int], debug_first: bool = False) -> list[dict]:
    all_metrics = []
    for i, s in enumerate(seeds):
        global _NOISE_RNG
        _NOISE_RNG = random.Random(s + 1000)
        m = run_test(seed=s, debug=(debug_first and i == 0))
        all_metrics.append(m)
        print_report(m)
    return all_metrics


if __name__ == "__main__":
    debug_flag = "--debug" in sys.argv
    seeds_to_run = [42, 7, 100, 2026, 9527]
    print(f"\n跑 {len(seeds_to_run)} 个 seed 测稳定性：{seeds_to_run}\n")
    results = _multi_seed_run(seeds_to_run, debug_first=debug_flag)

    print("\n" + "█" * 72)
    print("█  20K 多 seed 稳定性汇总")
    print("█" * 72)
    print(f"  {'Seed':>6}  {'Recall':>8}  {'Precision':>10}  {'F1':>8}  {'In report':>10}")
    for s, m in zip(seeds_to_run, results):
        print(f"  {s:>6}  {m['recall']:>7.1%}  {m['precision']:>9.1%}  {m['f1']:>7.1%}  {m['total_in_report']:>10}")

    avg_recall = sum(m["recall"] for m in results) / len(results)
    avg_prec   = sum(m["precision"] for m in results) / len(results)
    avg_f1     = sum(m["f1"] for m in results) / len(results)
    min_recall = min(m["recall"] for m in results)
    print(f"\n  平均 Recall    : {avg_recall:.1%}   最低: {min_recall:.1%}")
    print(f"  平均 Precision : {avg_prec:.1%}")
    print(f"  平均 F1        : {avg_f1:.1%}")

    target = 0.90
    if avg_recall >= target and min_recall >= target * 0.95:
        print(f"\n  ✓ 召回率多 seed 稳定达标（≥ {target:.0%}）")
        sys.exit(0)
    else:
        print(f"\n  ✗ 召回率未达标")
        sys.exit(1)
