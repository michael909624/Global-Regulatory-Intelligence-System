"""
Evaluate — 黄金集召回率评估。

读取 tests/gold_set.json,对照数据库现状,计算系统对已知应被发现法规的覆盖率。

匹配策略(任一命中即算召回):
  1. 标题 reg_hash 命中 raw_search_results 或 compliance_analysis
  2. 标题别名 reg_hash 命中
  3. URL 完全相同(归一化后)

输出:
  • 召回率(matched / total)
  • 按市场分组的召回明细
  • 漏掉的条目清单(下次优化的输入)
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from database import get_connection, init_db
from utils import reg_hash

GOLD_PATH = os.path.join(os.path.dirname(__file__), "tests", "gold_set.json")


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip().lower().rstrip("/")
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def _build_db_indexes() -> tuple[set[str], set[str]]:
    """返回 (DB 中所有 title hash 集合, DB 中所有归一化 URL 集合).

    URL 仅来自 raw_search_results.source_url。
    compliance_analysis 通过 content_hash 关联,只取 hash。
    """
    init_db()
    hashes: set[str] = set()
    urls: set[str]   = set()
    with get_connection() as conn:
        for row in conn.execute(
            "SELECT content_hash, source_url FROM raw_search_results"
        ):
            if row["content_hash"]:
                hashes.add(row["content_hash"])
            urls.add(_normalize_url(row["source_url"] or ""))
        for row in conn.execute(
            "SELECT content_hash FROM compliance_analysis"
        ):
            if row["content_hash"]:
                hashes.add(row["content_hash"])
    urls.discard("")
    return hashes, urls


def evaluate() -> dict:
    if not os.path.exists(GOLD_PATH):
        raise FileNotFoundError(
            f"找不到黄金集文件:{GOLD_PATH}\n"
            f"请先创建 tests/gold_set.json(可参考代码仓库内的模板)。"
        )

    with open(GOLD_PATH, encoding="utf-8") as f:
        gold = json.load(f)

    entries = gold.get("gold_entries", [])
    if not entries:
        raise ValueError("gold_set.json 的 gold_entries 为空,请先添加条目。")

    db_hashes, db_urls = _build_db_indexes()

    matched: list[dict] = []
    missed:  list[dict] = []

    for e in entries:
        title       = e.get("title", "").strip()
        aliases     = e.get("title_aliases") or []
        url_norm    = _normalize_url(e.get("url", ""))

        candidates  = [title] + [a for a in aliases if a]
        title_hashes = {reg_hash(c) for c in candidates if c}

        hit_by_title = bool(title_hashes & db_hashes)
        hit_by_url   = bool(url_norm and url_norm in db_urls)

        record = {
            **e,
            "hit_by_title": hit_by_title,
            "hit_by_url":   hit_by_url,
        }
        if hit_by_title or hit_by_url:
            matched.append(record)
        else:
            missed.append(record)

    total       = len(entries)
    n_matched   = len(matched)
    recall      = n_matched / total if total else 0.0

    # 按市场分组
    by_market: dict[str, dict] = {}
    for e in entries:
        m = e.get("market", "未指定")
        by_market.setdefault(m, {"total": 0, "hit": 0})
        by_market[m]["total"] += 1
    for e in matched:
        by_market[e.get("market", "未指定")]["hit"] += 1

    return {
        "total":     total,
        "matched":   n_matched,
        "missed":    total - n_matched,
        "recall":    recall,
        "by_market": by_market,
        "missed_entries":  missed,
        "matched_entries": matched,
    }


def print_report(report: dict) -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              黄金集召回率评估                                ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print(f"  黄金集条目总数:  {report['total']}")
    print(f"  已发现:          {report['matched']}")
    print(f"  漏掉:            {report['missed']}")
    print(f"  召回率:          {report['recall']*100:.1f}%")
    print()

    if report["by_market"]:
        print("  按市场分组:")
        for m, st in sorted(report["by_market"].items()):
            r = st["hit"] / st["total"] if st["total"] else 0
            bar = "█" * int(r * 20) + "░" * (20 - int(r * 20))
            print(f"    {m:<10} {bar} {st['hit']}/{st['total']}  ({r*100:.0f}%)")
        print()

    if report["missed_entries"]:
        print("  ── 漏掉的条目(优化目标) ──")
        for e in report["missed_entries"]:
            print(f"    ✗ [{e.get('market','?')}] {e.get('title','')[:70]}")
            if e.get("source"):
                print(f"        来源:{e['source']}")
        print()
    else:
        print("  全部命中,召回率 100%。可以扩充黄金集继续提升标准。\n")

    if report["matched_entries"]:
        print("  ── 已命中的条目 ──")
        for e in report["matched_entries"]:
            tag = []
            if e.get("hit_by_url"):   tag.append("URL")
            if e.get("hit_by_title"): tag.append("标题")
            print(f"    ✓ [{'+'.join(tag)}] {e.get('title','')[:70]}")
        print()


def run_evaluate_command() -> None:
    """CLI 入口:python gris.py evaluate"""
    try:
        report = evaluate()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n  {e}\n")
        return
    print_report(report)


if __name__ == "__main__":
    run_evaluate_command()
