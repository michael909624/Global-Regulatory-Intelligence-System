"""
对抗式长尾召回率测试：10 类红队攻击向量 + 真实 LLM 模拟器。

与 test_recall_5k / 20k 的关键区别：
  • LLM 模拟器（tests.fixtures.llm_simulator）按 prompt 实际语义判断，不偷看 ground truth
  • Pool 设计针对当前 pipeline 已知短板：reg_id 异写 / truncate 盲区 / keeper 抢占 /
    prompt injection / 多语言 / 远期 / title-content 错位 / RAW/ 兜底 等

评测口径：
  应抓回 = sum(每个 expected_in_report=True 的独立条目)
         + sum(每个 merge_group 至少有 1 条 expected_in_report=True)
  实际抓回 = 周报里命中 expected=True 的条目数（merge_group 算 1 个命中）
  Recall = 实际抓回 / 应抓回   目标 ≥ 85%

跑：python3 -m tests.test_recall_adversarial [--debug]
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import config  # noqa: E402
config.DATABASE_PATH = _TMP_DB

import ai_client  # noqa: E402
import consolidator  # noqa: E402
from database import init_db, get_connection, get_all_analyses  # noqa: E402
from utils import reg_hash  # noqa: E402

from tests.fixtures.synthetic_pool_adversarial import (  # noqa: E402
    AdversarialReg, generate_adversarial_pool,
)
from tests.fixtures.llm_simulator import simulate_llm, simulate_grounded  # noqa: E402


# ── 数据装载 ────────────────────────────────────────────────────────────────


def _load_pool(pool: list[AdversarialReg]) -> None:
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


def _load_scraped(pool: list[AdversarialReg]) -> None:
    """按 outcome 分流：ok 写完整 sc；mismatch 写短无关 sc；fail 不写。"""
    case_by_title = {p.title.strip().lower(): p for p in pool}
    now = datetime.now().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, title FROM raw_search_results
            WHERE consolidated_into IS NULL AND scrape_status = '待抓取'
        """).fetchall()
        sc_rows = []
        update_ids = []
        for r in rows:
            case = case_by_title.get((r["title"] or "").strip().lower())
            if case is None:
                full_text = r["title"] or ""
            else:
                full_text = case.full_text  # mismatch 直接写原始短文本
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


def _evaluate(pool: list[AdversarialReg]) -> dict:
    rows = get_all_analyses()
    in_report_titles: set[str] = set()
    for row in rows:
        in_report_titles.add((row["title"] or "").strip().lower())

    # 间接召回（被 Stage 0 合并的成员，keeper 在周报）
    with get_connection() as conn:
        merged = conn.execute("""
            SELECT id, title, consolidated_into FROM raw_search_results
            WHERE consolidated_into IS NOT NULL
        """).fetchall()
        for r in merged:
            keeper = conn.execute(
                "SELECT title FROM raw_search_results WHERE id=?",
                (r["consolidated_into"],),
            ).fetchone()
            if keeper:
                kt = (keeper["title"] or "").strip().lower()
                if kt in in_report_titles:
                    in_report_titles.add((r["title"] or "").strip().lower())

    # 按 attack_kind 分桶统计
    # 注意：merge_group 内 expected_out 的成员被合并到 keeper（间接召回 in_report_titles）
    # 在系统层面 = 合并掉了不会单独出现，不应算误收——只豁免被 keeper 吸收的情况。
    by_attack: dict[str, dict] = {}
    for c in pool:
        b = by_attack.setdefault(c.attack_kind, {
            "total": 0, "expected_in": 0, "expected_out": 0,
            "actual_in": 0, "wrong_captured": 0, "missed": 0,
        })
        b["total"] += 1
        in_rep = c.title.strip().lower() in in_report_titles
        if c.expected_in_report:
            b["expected_in"] += 1
            if in_rep:
                b["actual_in"] += 1
            else:
                b["missed"] += 1
        else:
            b["expected_out"] += 1
            if in_rep and c.merge_group is None:
                # 独立条目 + 进周报 = 真误收
                b["wrong_captured"] += 1

    # 应抓回分母 = 独立 expected_in 条目 + 每个 merge_group（至少 1 条 expected_in）算 1
    standalone_expected_in = 0
    merge_group_expected: dict[str, list[AdversarialReg]] = {}
    for c in pool:
        if c.merge_group:
            merge_group_expected.setdefault(c.merge_group, []).append(c)
        elif c.expected_in_report:
            standalone_expected_in += 1

    standalone_recalled = 0
    for c in pool:
        if c.merge_group is None and c.expected_in_report:
            if c.title.strip().lower() in in_report_titles:
                standalone_recalled += 1

    # 合并组：组内只要有 1 个 expected_in 进周报就算召回
    group_expected_total = 0
    group_recalled = 0
    group_wrong_keeper = 0  # 组内只有 expected_out 进周报（keeper 抢占成功）
    for gid, members in merge_group_expected.items():
        has_expected_in = any(m.expected_in_report for m in members)
        if not has_expected_in:
            continue
        group_expected_total += 1
        # 命中：组内任一 expected_in 直接进周报，或被合并到一个 expected_in 的 keeper
        in_rep_titles_in_group = [m for m in members if m.title.strip().lower() in in_report_titles]
        # 检查间接召回：通过 consolidated_into 关系
        group_titles_lower = {m.title.strip().lower() for m in members}
        with get_connection() as conn:
            for m in members:
                row = conn.execute("""
                    SELECT consolidated_into FROM raw_search_results
                    WHERE LOWER(TRIM(title))=?
                """, (m.title.strip().lower(),)).fetchone()
                if row and row["consolidated_into"]:
                    keeper_title_row = conn.execute(
                        "SELECT title FROM raw_search_results WHERE id=?",
                        (row["consolidated_into"],),
                    ).fetchone()
                    if keeper_title_row:
                        kt = (keeper_title_row["title"] or "").strip().lower()
                        if kt in in_report_titles and m.expected_in_report:
                            in_rep_titles_in_group.append(m)
        # 至少 1 个 expected_in 进了 → 召回成功
        if any(m.expected_in_report for m in in_rep_titles_in_group):
            group_recalled += 1
        else:
            # 检查是否仅 expected_out 进了
            if any(not m.expected_in_report for m in in_rep_titles_in_group):
                group_wrong_keeper += 1

    expected_total = standalone_expected_in + group_expected_total
    recalled_total = standalone_recalled + group_recalled

    # 误收
    wrong_captured_standalone = sum(
        1 for c in pool
        if (not c.expected_in_report) and (c.merge_group is None)
        and (c.title.strip().lower() in in_report_titles)
    )
    in_report_total = len(in_report_titles)

    recall = recalled_total / expected_total if expected_total else 0.0
    # precision 简化：周报总条数中应进的占比
    precision = recalled_total / in_report_total if in_report_total else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "by_attack":         by_attack,
        "expected_total":    expected_total,
        "recalled_total":    recalled_total,
        "standalone_recalled": standalone_recalled,
        "group_recalled":    group_recalled,
        "group_expected_total": group_expected_total,
        "group_wrong_keeper": group_wrong_keeper,
        "wrong_captured":    wrong_captured_standalone,
        "in_report_total":   in_report_total,
        "recall":            recall,
        "precision":         precision,
        "f1":                f1,
    }


# ── 主入口 ──────────────────────────────────────────────────────────────────


def _patched_call_json(prompt: str, *, system: str = "", **kwargs) -> str:
    return simulate_llm(prompt, system=system)


def _patched_call_grounded(prompt: str, **kwargs) -> tuple[str, list]:
    return simulate_grounded(prompt, **kwargs)


def run_test(seed: int = 42, debug: bool = False) -> dict:
    print(f"\n{'='*72}")
    print(f"  GRIS 对抗式长尾压测  seed={seed}")
    print(f"  LLM 角色：tests.fixtures.llm_simulator（语义启发式 + 真实噪声）")
    print(f"{'='*72}")

    t0 = time.time()
    pool = generate_adversarial_pool(seed=seed)
    print(f"\n  [生成] {len(pool)} 条对抗 pool ({time.time()-t0:.2f}s)")

    from collections import Counter
    counts = Counter(p.attack_kind for p in pool)
    for k, v in sorted(counts.items()):
        print(f"    {k:<32} : {v}")

    # patch
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

        t0 = time.time()
        consolidator.consolidate_pending(verbose=False)
        t_stage0 = time.time() - t0

        t0 = time.time()
        _load_scraped(pool)
        t_scrape = time.time() - t0

        t0 = time.time()
        from analyzer import run_analysis
        run_analysis(skip_fallback=False)
        t_analyze = time.time() - t0

        t0 = time.time()
        metrics = _evaluate(pool)
        t_eval = time.time() - t0

        peak = tracemalloc.get_traced_memory()[1]
        metrics["_perf"] = {
            "load_db":         t_load,
            "stage0":          t_stage0,
            "load_scraped":    t_scrape,
            "analyze":         t_analyze,
            "evaluate":        t_eval,
            "total":           t_load + t_stage0 + t_scrape + t_analyze + t_eval,
            "memory_peak_mb":  peak / 1024 / 1024,
        }

        if debug:
            _debug_failures(pool)

        return metrics
    finally:
        tracemalloc.stop()
        ai_client.call_json = original_call_json
        ai_client.call_grounded = original_call_grounded


def _debug_failures(pool: list[AdversarialReg]) -> None:
    rows = get_all_analyses()
    in_report = {(r["title"] or "").strip().lower() for r in rows}

    print("\n  ── 漏球（expected_in=True 但未进周报）──")
    for c in pool:
        if c.expected_in_report and c.title.strip().lower() not in in_report:
            with get_connection() as conn:
                row = conn.execute("""
                    SELECT rs.scrape_status, rs.consolidated_into,
                           ca.affected_products, ca.business_dimensions, ca.impact_level
                    FROM raw_search_results rs
                    LEFT JOIN scraped_content sc ON sc.raw_id = rs.id
                    LEFT JOIN compliance_analysis ca ON ca.scraped_id = sc.id
                    WHERE rs.title=?
                """, (c.title,)).fetchone()
                # 检查间接召回
                indirect_hit = False
                if row and row["consolidated_into"]:
                    keeper = conn.execute(
                        "SELECT title FROM raw_search_results WHERE id=?",
                        (row["consolidated_into"],),
                    ).fetchone()
                    if keeper and (keeper["title"] or "").strip().lower() in in_report:
                        indirect_hit = True
                if indirect_hit:
                    continue  # 间接召回不算漏
                tag = f"[{c.attack_kind:<28}]"
                line = f"    {tag} {c.title[:75]}"
                if row:
                    line += (f"\n      status={row['scrape_status']} "
                             f"into={row['consolidated_into']} "
                             f"prod={row['affected_products']!r} "
                             f"dim={row['business_dimensions']}")
                print(line)
                if c.notes:
                    print(f"      ℹ {c.notes}")

    print("\n  ── 误收（expected_in=False 但进了周报）──")
    for c in pool:
        if (not c.expected_in_report) and c.title.strip().lower() in in_report:
            print(f"    [{c.attack_kind:<28}] {c.title[:75]}")
            if c.notes:
                print(f"      ℹ {c.notes}")


def print_report(m: dict) -> None:
    print("\n" + "=" * 72)
    print("                   对抗式长尾压测报告")
    print("=" * 72)
    print(f"  ★ Recall    : {m['recall']:.1%}  ({m['recalled_total']}/{m['expected_total']})")
    print(f"  ★ Precision : {m['precision']:.1%}  ({m['recalled_total']}/{m['in_report_total']})")
    print(f"  ★ F1        : {m['f1']:.1%}")
    print()
    print(f"  独立应抓回         : {m['standalone_recalled']}")
    print(f"  合并组应抓回 (各 1) : {m['group_recalled']}/{m['group_expected_total']}")
    print(f"  组内 keeper 被抢占  : {m['group_wrong_keeper']}（A6 攻击成功数）")
    print(f"  误收（应排除但进入）: {m['wrong_captured']}")
    print()
    print(f"  各 attack 类拦截 / 召回情况：")
    for kind, b in sorted(m["by_attack"].items()):
        if b["expected_in"] > 0:
            ratio = b["actual_in"] / b["expected_in"]
            mark = "✓" if ratio >= 0.85 else "✗"
            print(f"    {mark} {kind:<32} 召回 {b['actual_in']:>3}/{b['expected_in']:<3} ({ratio:.1%})")
        else:
            wrong = b["wrong_captured"]
            mark = "✓" if wrong / max(b['total'], 1) <= 0.05 else "✗"
            print(f"    {mark} {kind:<32} 误收 {wrong:>3}/{b['total']:<3} "
                  f"({wrong / max(b['total'], 1):.1%})")
    print()
    p = m["_perf"]
    print(f"  性能：装载 {p['load_db']:.2f}s | Stage0 {p['stage0']:.2f}s | "
          f"sc {p['load_scraped']:.2f}s | analyze {p['analyze']:.2f}s | "
          f"total {p['total']:.2f}s | mem {p['memory_peak_mb']:.0f} MB")
    print("=" * 72)


if __name__ == "__main__":
    debug = "--debug" in sys.argv
    seeds = [42, 7, 100]
    print(f"\n跑 {len(seeds)} 个 seed：{seeds}\n")
    results = []
    for i, s in enumerate(seeds):
        m = run_test(seed=s, debug=(debug and i == 0))
        results.append(m)
        print_report(m)

    print("\n" + "█" * 72)
    print("█  对抗压测多 seed 汇总")
    print("█" * 72)
    print(f"  {'Seed':>6}  {'Recall':>8}  {'Precision':>10}  {'F1':>8}  {'Wrong':>6}")
    for s, m in zip(seeds, results):
        print(f"  {s:>6}  {m['recall']:>7.1%}  {m['precision']:>9.1%}  {m['f1']:>7.1%}  "
              f"{m['wrong_captured']:>6}")

    avg_recall = sum(m["recall"] for m in results) / len(results)
    min_recall = min(m["recall"] for m in results)
    avg_prec = sum(m["precision"] for m in results) / len(results)
    print(f"\n  平均 Recall    : {avg_recall:.1%}   最低: {min_recall:.1%}")
    print(f"  平均 Precision : {avg_prec:.1%}")

    target = 0.85
    if avg_recall >= target:
        print(f"\n  ✓ 平均 Recall ≥ {target:.0%}（用户可接受阈值）")
        sys.exit(0)
    else:
        print(f"\n  ✗ 平均 Recall 未达 {target:.0%}，需要分析根因")
        sys.exit(1)
