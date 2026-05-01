"""
早期 AI 预筛：raw_search_results 抓回后、scraper 介入前批量判定。

定位：在 researcher 召回后，立即让 LLM 看一遍标题清单，判定每条
"是否值得跑下游全流程"。drop 的不进 scraper / analyzer / fallback，
能砍掉 30-50% 低价值候选，省下下游最贵的调用（main analyzer + Gemini 合成）。

设计原则：
  • 仅看标题 + 市场 + 议题维度（不需要正文 — 标题足够判定明显噪音）
  • 用最便宜的 gemini-2.5-flash-lite + thinking_budget=0
  • 单次批量调用 ~$0.001-0.005（对比下游每条 $0.025+ 的节省）
  • 失败时全部默认 pursue（保守不漏球）
  • few-shot 例子在 prompts/llm_triage_system.txt（用户可读可改）

调用链：researcher.run() 末尾 → triage_pending() → 写回
        raw_search_results.triage_decision / triage_reason
        scraper 取候选时过滤 triage_decision != 'drop'
"""
from __future__ import annotations

import json

import ai_client
import prompts
from database import get_connection
from utils import get_logger, parse_json_array

_log = get_logger("llm_triage")

_MODEL = "gemini-2.5-flash-lite"
_TRIAGE_SYSTEM = prompts.load("llm_triage_system")

# 单批最大候选数。批太大 LLM 上下文会失控；批太小调用次数多。
# 100 条标题（每条 ~50 字）≈ 5000 input tokens，单次成本 ~$0.0005，可接受。
_BATCH_SIZE = 100


def _format_entry(row) -> str:
    """精简候选行：id + 市场 + reg_id + 标题（≤80 字）。"""
    rid = row["id"]
    market = (row["market"] or row["title"][:0] or "?")[:25]  # market 字段；缺时用 ?
    reg_id = (row["reg_id"] or "—")[:40]
    title = (row["title"] or "").strip()[:120]
    return f"[{rid}] [{market}] reg_id={reg_id!r} 标题={title!r}"


def _select_pending(conn, limit: int | None = None) -> list:
    """取"待预筛"的 raw 行：

    条件：
      • triage_decision IS NULL（未判定过）
      • consolidated_into IS NULL（未被 Stage 0 合并）
      • scrape_status = '待抓取'（仅处理尚未走下游的；已抓取/失败/需人工的
        都已经投入过下游成本，重判 triage 无意义）
      • title 非空
    """
    sql = """
        SELECT id, title, market, reg_id, snippet
        FROM raw_search_results
        WHERE triage_decision IS NULL
          AND consolidated_into IS NULL
          AND scrape_status = '待抓取'
          AND title IS NOT NULL
          AND TRIM(title) != ''
        ORDER BY id
    """
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    return list(conn.execute(sql).fetchall())


def _judge_batch(rows: list) -> dict[int, tuple[str, str | None, str]]:
    """对一个 batch 调一次 LLM，返回 {raw_id: (decision, level, reason)}"""
    if not rows:
        return {}

    entries = "\n".join(_format_entry(r) for r in rows)
    prompt = f"候选清单（共 {len(rows)} 条）：\n\n{entries}\n\n请输出 JSON 判定。"

    try:
        resp = ai_client.call_json(
            prompt, system=_TRIAGE_SYSTEM,
            model=_MODEL, thinking_budget=0,
        )
    except Exception as e:
        _log.warning("triage batch (%d 条) 调用失败: %s — 全部按 pursue 兜底", len(rows), e)
        # 失败兜底：全部 pursue（不漏球）
        return {r["id"]: ("pursue", None, "LLM 调用失败兜底") for r in rows}

    parsed = parse_json_array(resp) or []
    valid_ids = {r["id"] for r in rows}
    out: dict[int, tuple[str, str | None, str]] = {}

    for item in parsed:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        decision = item.get("decision")
        level = item.get("level")
        reason = (item.get("reason") or "")[:120]

        if not isinstance(rid, int) or rid not in valid_ids:
            continue
        if decision not in ("pursue", "drop"):
            continue
        if level is not None and level not in ("L1", "L2"):
            level = None
        out[rid] = (decision, level, reason)

    # LLM 漏掉的条目兜底标 pursue
    for r in rows:
        if r["id"] not in out:
            out[r["id"]] = ("pursue", None, "LLM 输出缺漏兜底")

    return out


def triage_pending(verbose: bool = True) -> tuple[int, int]:
    """对所有 triage_decision IS NULL 的 raw 批量预筛。

    返回 (pursued, dropped)。
    """
    with get_connection() as conn:
        pending = _select_pending(conn)

    if not pending:
        if verbose:
            print("  没有待预筛的 raw 条目。")
        return 0, 0

    if verbose:
        print(f"\n  📋 待预筛 {len(pending)} 条（批大小 {_BATCH_SIZE}）")

    pursued = dropped = 0
    for i in range(0, len(pending), _BATCH_SIZE):
        batch = pending[i : i + _BATCH_SIZE]
        results = _judge_batch(batch)

        # 写回 DB
        with get_connection() as conn:
            for rid, (decision, level, reason) in results.items():
                # level 暂存到 reason 头部（便于 llm_priority 后续参考）
                tag = f"[{level}] " if level else ""
                conn.execute(
                    "UPDATE raw_search_results "
                    "SET triage_decision=?, triage_reason=? WHERE id=?",
                    (decision, f"{tag}{reason}", rid),
                )
                if decision == "pursue":
                    pursued += 1
                else:
                    dropped += 1

        if verbose:
            print(f"    batch {i // _BATCH_SIZE + 1}: {len(batch)} 条 "
                  f"→ {sum(1 for d, _, _ in results.values() if d == 'pursue')} pursue / "
                  f"{sum(1 for d, _, _ in results.values() if d == 'drop')} drop")

    if verbose:
        ratio = (dropped / (pursued + dropped) * 100) if (pursued + dropped) else 0
        print(f"\n  ✓ 预筛完成：{pursued} pursue / {dropped} drop ({ratio:.1f}% 砍掉)")

    return pursued, dropped


if __name__ == "__main__":
    import ai_client as _ai
    _ai.reset_token_stats()
    triage_pending()
    _ai.print_token_summary("  ")
