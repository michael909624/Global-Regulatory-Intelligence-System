"""
末端 AI 终审：reporter 渲染前批量判定 L1/L2 + P0/P1。

定位：取代 priority.py 启发式（关键词 + 阈值 + 强保护）作为主判定。
priority.py 退化为兜底（仅 LLM 失败时启用）。

设计原则：
  • 横向看所有候选 — 比单条独立分析更准（"加州这 5 条 SB 都该 P0 吗？"）
  • 用 Gemini 3 Flash + thinking_budget=1024 — P0/P1 是综合判断,
    lite 关 thinking 在这种综合任务上欠配置;flash + 适度 thinking 准确度显著提升,
    批量调用绝对成本仅 ~$0.05-0.15/run。
  • 失败时降级（什么都不写 → reporter 走 priority.score_fallback）
  • few-shot 例子在 prompts/llm_priority_system.txt（用户可读可改）

调用链：reporter.generate_report() 调用前 → judge_pending() →
        写回 compliance_analysis.ai_level / ai_priority / ai_reason
        reporter 优先读 ai_priority 字段，缺失才走 priority 兜底
"""
from __future__ import annotations

import json
from datetime import datetime

import ai_client
import prompts
from database import get_connection
from utils import get_logger, parse_json_array

_log = get_logger("llm_priority")

_MODEL = "gemini-3-flash-preview"
_PRIORITY_SYSTEM = prompts.load("llm_priority_system")

# 单批最大候选数。lite 上下文宽，每条 ~150 字 × 60 条 ~ 9k tokens 可控。
_BATCH_SIZE = 60


def _format_entry(row) -> str:
    """精简候选行：id + 市场 + reg_id + key_dates 摘要 + 简短 req + L2 提示。"""
    rid = row["id"]
    market = (row["affected_markets"] or "?")[:30]
    reg_id = (row["reg_id"] or "—")[:50]
    title = (row["title_cn"] or row["title"] or "").strip()[:120]
    products = (row["affected_products"] or "?")[:60]

    # key_dates 摘要：取 publish + effective + 第一个 enforcement
    kd_summary = ""
    try:
        kd = json.loads(row["key_dates"] or "{}")
        parts = []
        if kd.get("publish"): parts.append(f"pub={kd['publish']}")
        if kd.get("effective"): parts.append(f"eff={kd['effective']}")
        enfs = kd.get("enforcements") or []
        if enfs and isinstance(enfs[0], dict) and enfs[0].get("date"):
            parts.append(f"enf={enfs[0]['date']}")
        if len(enfs) > 1:
            parts.append(f"+{len(enfs)-1}日期")
        kd_summary = " ".join(parts)
    except Exception:
        pass

    req = (row["compliance_requirement"] or "").replace("\n", " ")[:100]

    return (
        f"[{rid}] [{market}] reg_id={reg_id!r}\n"
        f"      title: {title!r}\n"
        f"      products={products!r} {kd_summary}\n"
        f"      req: {req!r}"
    )


def _select_pending(conn, limit: int | None = None) -> list:
    """取所有 ai_priority IS NULL 且未被合并的 compliance_analysis 行。

    向 LLM 提供：title / title_cn / reg_id / affected_markets / affected_products /
                key_dates / compliance_requirement
    """
    sql = """
        SELECT ca.id,
               rs.title, rs.title_cn, rs.reg_id,
               ca.affected_markets, ca.affected_products,
               ca.key_dates, ca.compliance_requirement
        FROM compliance_analysis ca
        JOIN scraped_content sc ON sc.id = ca.scraped_id
        JOIN raw_search_results rs ON rs.id = sc.raw_id
        WHERE ca.ai_priority IS NULL
          AND rs.consolidated_into IS NULL
        ORDER BY ca.id
    """
    if limit:
        sql += f"\nLIMIT {int(limit)}"
    return list(conn.execute(sql).fetchall())


def _judge_batch(rows: list, today: str) -> dict[int, tuple[str, str, str]]:
    """对一个 batch 调一次 LLM，返回 {ca_id: (level, priority, reason)}"""
    if not rows:
        return {}

    entries = "\n\n".join(_format_entry(r) for r in rows)
    prompt = (
        f"今日日期：{today}\n\n"
        f"候选清单（共 {len(rows)} 条已分析过的法规）：\n\n"
        f"{entries}\n\n"
        f"请输出 JSON 判定数组（无 markdown 标记）。"
    )

    try:
        resp = ai_client.call_json(
            prompt, system=_PRIORITY_SYSTEM,
            model=_MODEL, thinking_budget=1024,
        )
    except Exception as e:
        _log.warning("priority batch (%d 条) 调用失败: %s — 留空交 priority 兜底",
                     len(rows), e)
        return {}

    parsed = parse_json_array(resp) or []
    valid_ids = {r["id"] for r in rows}
    out: dict[int, tuple[str, str, str]] = {}

    for item in parsed:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        level = item.get("level")
        priority = item.get("priority")
        reason = (item.get("reason") or "")[:120]

        if not isinstance(cid, int) or cid not in valid_ids:
            continue
        if level not in ("L1", "L2", "drop"):
            continue
        if priority not in ("P0", "P1", "drop"):
            continue

        # 一致性检查：level=drop 必须 priority=drop（反之亦然）
        if (level == "drop") != (priority == "drop"):
            # 不一致 → 取保守 P1 而非 drop
            level = level if level != "drop" else "L2"
            priority = "P1"
            reason = f"[一致性兜底] {reason}"

        out[cid] = (level, priority, reason)

    return out


def judge_pending(verbose: bool = True) -> int:
    """对所有 ai_priority IS NULL 的 ca 批量终审。

    返回处理条数（成功写回的）。失败/未判定的留空，下游 priority 兜底。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        pending = _select_pending(conn)

    if not pending:
        if verbose:
            print("  没有待终审的 compliance_analysis 条目。")
        return 0

    if verbose:
        print(f"\n  ⚖️  待终审 {len(pending)} 条（批大小 {_BATCH_SIZE}）")

    total_judged = 0
    summary = {"L1 P0": 0, "L2 P0": 0, "P1": 0, "drop": 0}

    for i in range(0, len(pending), _BATCH_SIZE):
        batch = pending[i : i + _BATCH_SIZE]
        results = _judge_batch(batch, today)

        with get_connection() as conn:
            for cid, (level, priority, reason) in results.items():
                conn.execute(
                    "UPDATE compliance_analysis "
                    "SET ai_level=?, ai_priority=?, ai_reason=? WHERE id=?",
                    (level, priority, reason, cid),
                )
                total_judged += 1
                if priority == "drop":
                    summary["drop"] += 1
                elif priority == "P1":
                    summary["P1"] += 1
                else:  # P0
                    key = f"{level} P0"
                    summary[key] = summary.get(key, 0) + 1

        if verbose:
            print(f"    batch {i // _BATCH_SIZE + 1}: {len(batch)} 条 "
                  f"→ 已判定 {len(results)} / 兜底 {len(batch) - len(results)}")

    if verbose:
        print(f"\n  ✓ 终审完成：{total_judged}/{len(pending)} 条已写回 ai_*")
        print(f"    分布：{summary}")

    return total_judged


if __name__ == "__main__":
    import ai_client as _ai
    _ai.reset_token_stats()
    judge_pending()
    _ai.print_token_summary("  ")
