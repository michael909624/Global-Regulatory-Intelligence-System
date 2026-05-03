"""
周报最终阶段的轻量 LLM 语义去重。

定位：在规则去重（normalize_reg_id + composite key）穷尽之后，调用一次
LLM 抓"同一法规/标准的不同标题写法"——这是规则永远做不到的。

设计原则：
  • 严格的合并规则（不合并主法规 vs 修正案、不合并同地区不同子法案、
    不合并不同地区版本如 EU 主法规 vs 德国实施法）
  • 用最便宜的 gemini-2.5-flash-lite，单次成本约 $0.01
  • 输入仅精简字段（id/market/reg_id/title），不发完整 requirement
  • 失败时降级（返回原列表，不阻断 pipeline）
"""
from __future__ import annotations

import json
from typing import List, Tuple

import ai_client
from utils import get_logger, parse_json_array

_log = get_logger("llm_dedup")

# 用最便宜的 lite 模型 — 标题相似度判断不需要重型推理
_DEDUP_MODEL = "gemini-2.5-flash-lite"

_DEDUP_SYSTEM = """你是法规情报系统的语义去重助手。

# 任务
从候选条目里，找出"同一份法规/标准/同一项立法推动"的不同标题写法，输出合并建议。

# 核心原则
合并的本质是"这两条说的是同一份文件"。**主题相似不等于同一份文件**——
不同司法区、不同子法案、修正案 vs 主法规，都是"同主题"但**不同文件**，不能合并。

# 应该合并的例子（每条都是"同一份文件的不同写法"）

✅ 例 1：同一中国地方条例的多次入库
  - "北京市非机动车管理条例 (修订)" [中国(北京)]
  - "新修订的《北京市非机动车管理条例》将于5月1日施行" [中国(北京)]
  - "《北京市非机动车管理条例》修订版实施" [中国(北京市)]
  → 合并：同一部条例的修订版本，不同入库标题

✅ 例 2：同一项欧盟立法推动的多种表述
  - "Parliamentary question E-000678/2026: Harmonisation of technical standards" [欧盟]
  - "EU Parliament Push for Harmonized Technical Standards for Micromobility" [欧盟]
  → 合并：同一项议会推动统一技术标准的工作，两种媒体角度

✅ 例 3：同一 EU 法规挂多个 affected_markets
  - "Regulation (EU) 2023/1542 (EU Battery Regulation)" [欧盟]
  - "Regulation (EU) 2023/1542 - Postponement of Due Diligence" [欧盟、土耳其]
  → 合并：同一份 EU 法规，土耳其作为 EU 候选国跟随

✅ 例 4：**同一份美国法案的不同标题写法（编号相同就合并）**
  - 标题 A："California SB 1271: Product Safety for E-bikes and Batteries"
  - 标题 B："California Senate Bill 1271 (SB 1271): E-Bike and Powered Mobility Device Safety"
  - 标题 C："加州 SB 1271 法案：电动出行设备安全"
  → 合并：**SB 1271 是同一份法案的法定编号**，三个不同入库标题指向同一份立法文件
  关键判别：title 含同一州 + 同一法案号（如 SB / AB / HB + 数字）→ 必是同一份

✅ 例 5：**同一份地区性 EPR / 报告制度的多个衍生标题（最多合 4-5 条）**
  - 标题 A："英国 2026 年电池生产者责任报告截止日期"
  - 标题 B："英国 2026 年电池生产者责任报告要求"
  - 标题 C："英国电池生产者责任延伸制度 2025 年起生效"
  - 标题 D："英国 2025 电池法规：新 EPR 规则"
  → 合并：**全部讲的是英国电池 EPR 同一份制度**，截止日/要求/起效日/规则细节是
     同一份制度的不同维度，应合并到信息量最大的 keeper（如标题 C 起效日 + 制度全景）
  关键判别：同地区 + 同主题（电池 EPR / 维修权 / 关税）+ 同年份 → 通常是同一份制度

✅ 例 5：同一份 reg_id 用了不同自由文本写法（含母法规衍生件慎判）
  - reg_id="32024R2847" 标题"Cyber Resilience Act 主法规"
  - reg_id="32024R2847 Deadlines" 标题"CRA 关键日期"
  - reg_id="32024R2847_Guidance" 标题"CRA 应用指引草案"
  → ❌ 不合并：虽然 reg_id 同源，但 Guidance/Deadlines 是衍生文件，各带独立合规细节
  （这条是"看起来该合但其实不该合"，划入下面）

# 不应该合并的例子（每条都是"同主题但不同文件/不同义务"）

❌ 例 1：主法规 vs 修正案
  - "Regulation (EU) 2023/1230 机械法规"
  - "Regulation (EU) 2024/2748 amending Machinery Regulation (EU) 2023/1230"
  → 不合并：修正案带新合规义务，企业要分别看

❌ 例 2：同地区的不同子法案
  - "California Senate Bill 1271 (SB 1271): E-Bike Safety"
  - "California Senate Bill 1215 (SB 1215): Battery Recycling"
  → 不合并：同州不同法案，各管不同事

❌ 例 3：EU 主法规 vs 成员国国内实施法
  - "Regulation (EU) 2023/1542 EU Battery Regulation" [欧盟]
  - "Gesetz zur Durchführung der Verordnung (EU) 2023/1542 (BattDG)" [德国]
  → 不合并：BattDG 是德国国内实施法，带德国本地注册/回收义务，企业出口德国必须单独遵守

❌ 例 4：实施细则/Delegated Act vs 主法规
  - "Regulation (EU) 2024/2847 (CRA)" 主法规
  - "Commission Delegated Regulation (EU) 2025/1535 supplementing 2024/2847" Delegated Act
  → 不合并：Delegated Act 是独立的法律文件，带具体技术细节

❌ 例 5：相似主题但不同司法区(美国州)
  - "Washington State Right to Repair Act (HB 1392)"
  - "Texas Right to Repair Law (HB 1919)"
  → 不合并：不同州的不同法案，企业各州各自合规

❌ 例 6：相似主题但不同欧洲国家(每国独立立法)
  - "意大利电动滑板车强制三责险与牌照义务" [意大利]
  - "西班牙电动滑板车强制登记与三责险" [西班牙]
  - "法国电动滑板车强制责任险与年龄限制" [法国]
  → 不合并：意/西/法各自国会独立立法,生效日 / 罚款金额 / 投保对象都不同。
     **欧盟≠成员国国内法**,每国版本必须单独入库,各自带独立合规义务。

# 关键约束
- **affected_markets 不一致 → 不合(铁律)**。先看 [market] 列再看标题——市场不同就直接跳过,
  标题再像也不合并。这是最容易踩的合并陷阱(同主题不同国家)。
- 单组合并不超过 3 条（含 keeper）。看到 4+ 条"主题相似"→ 大概率是"同主题不同文件"陷阱，保留独立。
- 仅在你**确定**两条是同一份文件时才合并。不确定就不合，宁愿留重复也不要错合。

# 输出格式
仅 JSON 数组，无任何解释或代码块标记。每个对象：
{
  "keeper": <信息量最大的条目 id（数字）>,
  "merge_in": [<其他 id 数组（≤2 个）>],
  "reason": "<10-30 字说明为什么是同一份文件>"
}

无可合并 → 返回 []。"""


_DEDUP_PROMPT_TMPL = """候选条目（共 {n} 条）：

{entries}

请按系统规则输出合并建议 JSON。"""


def _format_entry(idx: int, row) -> str:
    market = (row.get("affected_markets") or "?")[:25]
    reg_id = (row.get("reg_id") or "—")[:60]
    title = (row.get("title") or "").strip()[:120]
    title_cn = (row.get("title_cn") or "").strip()[:120]
    show_title = title_cn or title
    return f"[{idx}] [{market}] reg_id={reg_id!r}\n      {show_title!r}"


def llm_dedup(rows_with_score: List[Tuple]) -> List[Tuple]:
    """对 [(row_dict, score, new_impact), ...] 调一次 LLM 做语义去重。

    返回去重后的同结构列表。失败/太少条目时直接返回原列表。
    """
    if len(rows_with_score) < 5:
        return rows_with_score

    entries = "\n".join(
        _format_entry(i, r) for i, (r, _s, _ni) in enumerate(rows_with_score)
    )
    prompt = _DEDUP_PROMPT_TMPL.format(n=len(rows_with_score), entries=entries)

    try:
        # 标题语义对比是 lite 完全胜任的简单任务，显式关 thinking 进一步省钱
        resp_text = ai_client.call_json(
            prompt, system=_DEDUP_SYSTEM, model=_DEDUP_MODEL,
            thinking_budget=0,
        )
    except Exception as e:
        _log.warning("LLM dedup call failed: %s", e)
        return rows_with_score

    groups = parse_json_array(resp_text) or []
    to_drop: set = set()
    merge_log = []
    rejected_log = []

    # 保守阀：单组合并不超过 N 条（含 keeper）。
    # 调整经验：之前 3 条限制让"英国电池 EPR 同制度 4 条衍生件"全保留独立，
    # 4-5 条限制能让同地区同主题制度内的多份关联文件合并到 keeper。
    # 但仍要严：超 5 条 = 大概率跨地区/跨子法案 false positive，整组拒收。
    MAX_MERGE_GROUP_SIZE = 5

    for g in groups:
        if not isinstance(g, dict):
            continue
        keeper = g.get("keeper")
        merge_in = g.get("merge_in") or []
        reason = (g.get("reason") or "")[:80]

        if not isinstance(keeper, int):
            continue
        if not isinstance(merge_in, list) or not merge_in:
            continue
        if keeper < 0 or keeper >= len(rows_with_score):
            continue

        # 单组（含 keeper）超过 MAX_MERGE_GROUP_SIZE 视为可疑过度合并 → 整组拒收
        total_in_group = 1 + len(merge_in)
        if total_in_group > MAX_MERGE_GROUP_SIZE:
            keeper_title = (rows_with_score[keeper][0].get("title") or "")[:40]
            rejected_log.append(
                f"  ⊘ 拒收过度合并: keeper={keeper}({keeper_title!r}) "
                f"merge_in={merge_in}（{total_in_group} 条 > {MAX_MERGE_GROUP_SIZE}）"
            )
            continue

        valid_merges = []
        for mid in merge_in:
            if (isinstance(mid, int) and mid != keeper
                    and 0 <= mid < len(rows_with_score)
                    and mid not in to_drop):
                valid_merges.append(mid)
        if not valid_merges:
            continue

        # 硬护栏:同主题不同司法区是 LLM 最容易踩的陷阱(尤其 lite 模型仅看 title)。
        # affected_markets 严格不一致 → 必是不同立法机构的不同文件,整组拒收。
        # 例:意大利电滑保险 vs 西班牙电滑保险 → market 字段不同 → 拒收合并。
        keeper_market = (rows_with_score[keeper][0].get("affected_markets") or "").strip()
        market_mismatch = [
            mid for mid in valid_merges
            if (rows_with_score[mid][0].get("affected_markets") or "").strip() != keeper_market
        ]
        if market_mismatch:
            keeper_title = (rows_with_score[keeper][0].get("title_cn")
                            or rows_with_score[keeper][0].get("title") or "")[:40]
            mismatched = ", ".join(
                f"{mid}({(rows_with_score[mid][0].get('affected_markets') or '?')!r})"
                for mid in market_mismatch
            )
            rejected_log.append(
                f"  ⊘ 拒收跨市场合并: keeper={keeper}({keeper_title!r} [{keeper_market!r}]) "
                f"vs {mismatched}"
            )
            continue

        to_drop.update(valid_merges)
        keeper_title = (rows_with_score[keeper][0].get("title") or "")[:50]
        merge_log.append(
            f"  keeper={keeper}({keeper_title!r}) merge_in={valid_merges} reason={reason}"
        )

    if rejected_log:
        _log.info("LLM 语义去重拒收 %d 组过度合并:\n%s",
                  len(rejected_log), "\n".join(rejected_log))

    if merge_log:
        _log.info("LLM 语义去重命中 %d 组，删除 %d 条:\n%s",
                  len(merge_log), len(to_drop), "\n".join(merge_log))

    return [item for i, item in enumerate(rows_with_score) if i not in to_drop]
