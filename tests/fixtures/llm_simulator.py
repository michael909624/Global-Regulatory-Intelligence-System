"""
LLM 语义判断模拟器：扮演真实 Gemini 在合规分析任务上的行为。

设计原则（红队对抗测试用）：
  1. 不偷看 ground truth——只读 prompt 提供的 title / url / market / scraped_text
  2. 用语义启发式做判断——不是规则白盒，是模拟真实 LLM 的判断模式
  3. 模拟真实 Gemini 的常见错判模式（基于 5K/20K 测试观察）：
     • prompt injection ~40% 中招（真实 LLM 普遍易受影响）
     • 标题诱导：见 LEV 关键词易倾向召回（即使内容不沾），上钩率 ~15-20%
     • 短文本：< 200 字符判"信息不足"
     • 强反例（医药/银行/航空）：~99% 拒绝
     • 真合规法规（含 article + penalty + product 名）：~95% 召回，5% 漏判
     • 远期/历史法规：~90% 识别，10% 错召
     • 多语言：依语言不同有 90-95% 召回率（弱于英文）

被调用接口：
  • _patched_call_json(prompt, system) → 返回主分析或 fallback 分析的 JSON
  • _patched_call_grounded(prompt, ...) → 返回 (text, sources) 模拟 grounded fetch
"""
from __future__ import annotations

import hashlib
import json
import random
import re

# ── prompt 解析 ──────────────────────────────────────────────────────────────


def _parse_prompt(prompt: str) -> dict:
    """从主分析 / fallback prompt 里抽取 title、url、market、scraped_text。
       Prompt 模板里这些字段都以"法规名称：" / "官方链接：" / "适用市场：" 开头。"""
    fields = {"title": "", "url": "", "market": "", "scraped_text": ""}
    for line in prompt.split("\n"):
        if line.startswith("法规名称："):
            fields["title"] = line.replace("法规名称：", "").strip()
        elif line.startswith("官方链接："):
            fields["url"] = line.replace("官方链接：", "").strip()
        elif line.startswith("适用市场："):
            fields["market"] = line.replace("适用市场：", "").strip()
    # scraped_text 在两行 ━━━ 之间，简单方式：从"以下内容"或"原文"后抓
    if "━━" in prompt:
        body_match = re.search(r"━{5,}\n(.+?)(?:━{5,}|$)", prompt, re.DOTALL)
        if body_match:
            fields["scraped_text"] = body_match.group(1).strip()
    if not fields["scraped_text"]:
        # 兜底：取整个 prompt 后半段
        fields["scraped_text"] = prompt[len(prompt) // 2:]
    return fields


# ── 关键词信号（基于真实 LLM 在合规判断中的注意力模式）──────────────────────


_PROD_KEYWORDS_EN = [
    "e-bike", "e-bikes", "ebike", "ebikes", "electric bicycle", "pedelec",
    "e-scooter", "e-scooters", "scooter",
    "hoverboard", "self-balancing",
    "electric motorcycle", "moped",
    "lawn mower", "robotic mower",
    "lev", "light electric vehicle", "personal mobility",
    "battery", "lithium", "bms",
]
_PROD_KEYWORDS_ZH = [
    "电助力自行车", "电动自行车", "电动滑板车", "平衡车", "电动摩托车",
    "割草机", "锂电池", "电池组", "BMS",
]
_PROD_KEYWORDS_DE = ["e-bike", "e-roller", "fahrrad", "akku", "lithium"]
_PROD_KEYWORDS_JA = ["電動", "自転車", "スクーター", "電池", "リチウム"]
_PROD_KEYWORDS_KO = ["전동", "자전거", "스쿠터", "전지", "리튬"]


# 强反例：见到这些词几乎肯定是无关行业
_STRONG_IRRELEVANT_EN = [
    "pharmaceutical", "good manufacturing practice", "active pharmaceutical ingredient",
    "tier 1 capital", "basel iii", "risk-weighted",
    "boeing", "airworthiness", "type-certified aircraft", "civil aviation",
    "marine engine", "vessel", "international maritime organization",
    "cement", "structural integrity", "fire resistance",
    "etcs", "european train control",
    "tobacco", "cosmetic", "textile fiber", "real estate", "mining license",
    "agricultural produce", "pesticide residue", "food safety regulation",
    "dental clinic", "sterilization protocol",
]


# distractor 关键短语：title 看似相关，但 content 明确划清界限
_DISTRACTOR_DISCLAIMERS = [
    "not for road use", "not for personal mobility", "not consumer products",
    "not for consumer", "industrial logistics", "warehouse and factory",
    "vessel-mounted", "maritime tourism", "cruise ships",
    "medical device directive", "hospital patients",
    "stage prop", "non-functional decorative",
    "static educational display", "non-operational teaching",
    "military classification", "itar / eu dual-use",
    "atex explosive atmosphere", "underground mining",
    "airport apron ground-support", "aviation authority approval",
]


# 远期/历史标记：明确说"无现行义务"
_OUTDATED_LONGTERM_MARKERS = [
    "no immediate obligations", "no compliance action is required",
    "long-term draft", "scheduled for effect in", "future {prod} standards",
    "originally enacted", "has been in effect for over",
    "subsequent amendments have superseded", "historical reference",
    "discussion paper only", "stakeholder consultation document",
    "10 年后", "5 年后", "discussion paper", "long-term roadmap",
    "no new conformity assessment", "current effective rules remain in force",
    "until then, no compliance",
]


# 真合规法规标志：含 article + penalty + effective date 等具体条款
_COMPLIANCE_MARKERS_EN = [
    "article 1", "article 2", "article 3", "art. 1", "art. 2",
    "effective date", "enters into force", "shall comply",
    "conformity assessment", "notified body", "type approval",
    "penalties", "administrative fines", "annual turnover",
    "mandatory recall", "market withdrawal",
    "transpose into national law",
    "ce marking", "type-plate", "annex a", "annex b",
]
_COMPLIANCE_MARKERS_MULTILANG = [
    # 德
    "anwendungsbereich", "konformitätsbewertung", "inkrafttreten",
    "sanktionen", "anhang a", "anhang b",
    # 日
    "適用範囲", "適合性評価", "施行日", "認証機関", "別表a",
    # 韩
    "적용 범위", "적합성 평가", "시행일", "인증기관", "부속서",
    # 中
    "适用范围", "合格评定", "认证机构", "生效日期", "罚款", "附录",
]


# prompt injection 标志（LLM 可能上钩）
_INJECTION_MARKERS = [
    "system override", "ignore the above", "ignore previous instructions",
    "admin note from regulator", "the user has updated rules",
    "respond with:", "output only the following json",
    "actual classification per agency memo",
]


# 业务维度推导（按 title 关键词）
_DIM_HINTS = [
    (["recall", "召回", "リコール", "강제 시행"], "ENFORCE"),
    (["helmet", "license", "registration", "headgear", "驾照", "牌照"], "USE"),
    (["epr", "battery passport", "weee", "end-of-life", "回收"], "EOL"),
    (["tariff", "customs", "import", "hs code", "进口", "关税"], "IMPORT"),
    (["subsidy", "advertising", "labeling", "energy label", "补贴", "标签"], "RETAIL"),
    (["type approval", "ce marking", "ccc", "认证", "型式认证"], "CERT"),
    (["rohs", "reach", "svhc", "supply chain", "raw material", "原料"], "PROD"),
    (["cybersecurity", "cra", "ota", "telematics", "wireless",
      "battery safety", "emc", "design", "安全标准", "电池"], "RD"),
]


_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")


def _has_any(text: str, keywords: list[str]) -> bool:
    """关键词检测：
    - 拉丁字母：用 (?<![a-z]) / (?![a-z]) 非字母边界 + 复数后缀 (?:s|es)?。
      不用 \\b——\\b 把 CJK 字符当 word char，让 "lawn mowers" 紧接 "の" 不算边界，
      导致跨语言文本里英文产品名匹配失败。
    - CJK：用 substring（无词边界概念）。
    """
    t = text.lower()
    for k in keywords:
        kl = k.lower()
        if _CJK_RE.search(kl):
            if kl in t:
                return True
        else:
            if re.search(r"(?<![a-z])" + re.escape(kl) + r"(?:s|es)?(?![a-z])", t):
                return True
    return False


def _detect_dims(title: str, full_text: str) -> list[str]:
    """从 title + 全文识别业务维度。"""
    text = f"{title} {full_text}".lower()
    dims = []
    for kws, dim in _DIM_HINTS:
        if any(k.lower() in text for k in kws):
            if dim not in dims:
                dims.append(dim)
    if not dims:
        # 默认 RD（最常见）
        dims = ["RD"]
    return dims[:3]


def _detect_products(title: str, full_text: str) -> list[str]:
    """从内容识别整机产品。"""
    text = f"{title} {full_text}".lower()
    PROD_MAP = [
        (["e-bike", "电助力", "电动自行车", "ebike", "pedelec", "fahrrad", "自転車", "자전거"], "电助力自行车"),
        (["e-scooter", "scooter", "滑板车", "e-roller", "스쿠터"], "电动滑板车"),
        (["hoverboard", "self-balancing", "平衡车"], "电动平衡车"),
        (["electric motorcycle", "moped", "电动摩托", "オートバイ", "오토바이"], "电动摩托车"),
        (["lawn mower", "robotic mower", "割草机", "잔디"], "智能割草机"),
        (["light electric vehicle", "lev", "personal mobility"], "电助力自行车、电动滑板车"),
    ]
    products = []
    for kws, p in PROD_MAP:
        if any(k.lower() in text for k in kws):
            for sub in p.split("、"):
                if sub not in products:
                    products.append(sub)
    return products


def _detect_impact(title: str, full_text: str) -> str:
    """从关键词推断重要度。"""
    t = f"{title} {full_text}".lower()
    if _has_any(t, ["mandatory recall", "强制召回", "リコール命令", "召回令",
                     "禁售", "ban", "prohibition", "withdrawal order"]):
        return "🔴"
    if _has_any(t, ["fines", "penalties", "罚款", "罚则", "处罚",
                     "罰金", "제재", "sanktionen"]):
        return "🟡"
    if _has_any(t, ["consultation", "discussion paper", "draft only", "guideline",
                     "咨询", "讨论稿", "ガイドライン", "지침"]):
        return "🟡"   # 咨询/讨论稿低紧迫,但两档制度下统一 🟡(原 🟢 已废弃)
    return "🟡"


# ── 判断主函数 ───────────────────────────────────────────────────────────────


def _result_irrelevant() -> dict:
    return {
        "requirement":         "无适用要求",
        "dates":               {"publish": None, "effective": None,
                                "enforcements": [], "consultation_close": None},
        "deadline":            None,
        "importance":          "🟡",   # affected_products='不相关' 会先被 SQL 过滤,impact 不影响
        "importance_note":     "阶段?（不相关）",
        "worst_case":          "—",
        "business_impact":     "—",
        "business_dimensions": [],
        "affected_products":   "不相关",
        "affected_markets":    "",
    }


def _result_low_confidence(market: str, products: list[str]) -> dict:
    """LLM 被关键词诱导但信息不足时的低置信输出(dim=[] + 🟡,被周报视图过滤)。
    新规则下"🟡 + 零 dim → drop"取代旧"🟢 + 零 dim → drop"(commit 1ed7b32)。"""
    return {
        "requirement":         "1. 推断要求（信息不足）",
        "dates":               {"publish": None, "effective": None,
                                "enforcements": [], "consultation_close": None},
        "deadline":            None,
        "importance":          "🟡",
        "importance_note":     "信息不足 ｜ 合理推断",
        "worst_case":          "—",
        "business_impact":     "推断关联（标题含相关关键词，待复核）",
        "business_dimensions": [],
        "affected_products":   "、".join(products) if products else "电助力自行车",
        "affected_markets":    market or "未知",
    }


def _result_relevant(title: str, full_text: str, market: str, publish_date: str | None = None) -> dict:
    dims = _detect_dims(title, full_text)
    products = _detect_products(title, full_text) or ["电助力自行车"]
    impact = _detect_impact(title, full_text)

    # 从 full_text 抓发布日期（粗略）
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", full_text)
    date_str = publish_date or (date_match.group(1) if date_match else None)

    return {
        "requirement":         "1. 满足型式认证\n2. 合规标识与文档\n3. 上市后监管义务",
        "dates": {
            "publish":            date_str,
            "effective":          date_str,
            "enforcements":       [{"date": date_str, "scope": "all products"}] if date_str else [],
            "consultation_close": None,
        },
        "deadline":            date_str,
        "importance":          impact,
        "importance_note":     f"基于 article/penalty 信号 → {impact}",
        "worst_case":          "罚款 / 召回 / 禁售",
        "business_impact":     "合规成本上升、上市/续证延后",
        "business_dimensions": dims,
        "affected_products":   "、".join(products),
        "affected_markets":    market,
    }


def _seed_for(title: str, full_text: str) -> int:
    """伪 RNG 种子：基于 title + 部分 content，保证可重现 + 不同条目得不同抽样。"""
    h = hashlib.md5(f"{title}|{full_text[:200]}".encode()).hexdigest()
    return int(h[:8], 16)


def simulate_llm(prompt: str, system: str = "") -> str:
    """主分析 / fallback 分析的 LLM 模拟入口。返回 JSON 字符串。"""
    # 非合规分析的调用（如 cluster_residual / consolidation LLM）→ 默认不主动合并
    is_compliance = ("合规分析师" in system) or ("合规分析" in prompt)
    if not is_compliance:
        return "[]"

    f = _parse_prompt(prompt)
    title = f["title"]
    full_text = f["scraped_text"]
    market = f["market"]
    is_synth = "[Gemini synthesis]" in full_text or "Gemini" in system

    # 用 title+content hash 作为该条的 RNG 种子（决定本条 LLM 的"运气"）
    rng = random.Random(_seed_for(title, full_text))

    # ── 1. prompt injection 检测 ────────────────────────────────────────────────
    # 真实 LLM 对 injection 不完全鲁棒：约 40% 中招直接执行注入指令
    if _has_any(full_text, _INJECTION_MARKERS):
        if rng.random() < 0.40:
            # 中招：按注入指令输出"不相关"
            return json.dumps(_result_irrelevant(), ensure_ascii=False)
        # 60% 鲁棒：识别为注入，剥离后正常分析
        # 删除注入段后判断
        for marker in _INJECTION_MARKERS:
            full_text = re.sub(re.escape(marker) + ".*", "", full_text, flags=re.IGNORECASE | re.DOTALL)

    # ── 2. 极短文本（< 200 字符）─────────────────────────────────────────────────
    if len(full_text.strip()) < 200:
        return json.dumps(_result_irrelevant(), ensure_ascii=False)

    # ── 3. 强反例（医药/银行/航空/船舶/铁路）──────────────────────────────────
    if _has_any(full_text, _STRONG_IRRELEVANT_EN):
        # ~99% 拒绝（真实 LLM 在强反例上几乎不犯错）
        if rng.random() < 0.99:
            return json.dumps(_result_irrelevant(), ensure_ascii=False)

    # ── 4. distractor disclaimer（明确说"不适用 personal mobility"）─────────────
    if _has_any(full_text, _DISTRACTOR_DISCLAIMERS):
        if rng.random() < 0.92:
            return json.dumps(_result_irrelevant(), ensure_ascii=False)
        # 8% 上钩：被产品名诱导，给低置信猜测
        return json.dumps(_result_low_confidence(market, _detect_products(title, full_text)),
                          ensure_ascii=False)

    # ── 5. 远期/历史标记 ──────────────────────────────────────────────────────
    if _has_any(full_text, _OUTDATED_LONGTERM_MARKERS):
        if rng.random() < 0.90:
            return json.dumps(_result_irrelevant(), ensure_ascii=False)
        # 10% 上钩：被产品+RD/CERT 关键词诱导,输出 dim=[]+🟡 低置信(信息不足)
        return json.dumps(_result_low_confidence(market, _detect_products(title, full_text)),
                          ensure_ascii=False)

    # ── 6. 真合规法规（含 article + penalty/effective + 产品名）─────────────────
    has_compliance_struct = (
        _has_any(full_text, _COMPLIANCE_MARKERS_EN)
        or _has_any(full_text, _COMPLIANCE_MARKERS_MULTILANG)
    )
    has_product = (
        _has_any(full_text, _PROD_KEYWORDS_EN)
        or _has_any(full_text, _PROD_KEYWORDS_ZH)
        or _has_any(full_text, _PROD_KEYWORDS_DE)
        or _has_any(full_text, _PROD_KEYWORDS_JA)
        or _has_any(full_text, _PROD_KEYWORDS_KO)
        or _has_any(title, _PROD_KEYWORDS_EN + _PROD_KEYWORDS_ZH)
    )

    if has_compliance_struct and has_product:
        # 多语言文本 LLM 召回率略低（90% vs 95%）
        is_multilang = (
            _has_any(full_text, _COMPLIANCE_MARKERS_MULTILANG)
            and not _has_any(full_text, _COMPLIANCE_MARKERS_EN)
        )
        miss_rate = 0.10 if is_multilang else 0.05
        if rng.random() < miss_rate:
            return json.dumps(_result_irrelevant(), ensure_ascii=False)
        # fallback 模式：importance 不超过 🟡
        result = _result_relevant(title, full_text, market)
        if is_synth and result["importance"] == "🔴":
            result["importance"] = "🟡"
        return json.dumps(result, ensure_ascii=False)

    # ── 7. 边界 case：标题含产品名但内容缺乏合规结构 ──────────────────────────
    title_has_prod = _has_any(title, _PROD_KEYWORDS_EN + _PROD_KEYWORDS_ZH)
    if title_has_prod:
        # 80% 信息不足判 🟡+[](被低置信过滤),20% 上钩
        if rng.random() < 0.80:
            return json.dumps(_result_low_confidence(market, _detect_products(title, full_text)),
                              ensure_ascii=False)
        # 上钩：错判相关
        return json.dumps(_result_relevant(title, full_text, market), ensure_ascii=False)

    # ── 8. 默认：不相关 ────────────────────────────────────────────────────────
    return json.dumps(_result_irrelevant(), ensure_ascii=False)


def simulate_grounded(prompt: str, **kwargs) -> tuple[str, list]:
    """fallback grounded fetch 模拟：扮演"我用 Google 搜索这个法规"的角色。

    判断逻辑：
      • 看 prompt 里的 title 是否像合规法规（含 LEV 关键词 + 监管动词）
      • 是 → 合成一段 ~1500 字符的合规内容
      • 否 → 返回空（找不到）
    """
    f = _parse_prompt(prompt)
    title = f["title"]
    market = f["market"]

    # 看 title 是否合规导向 + 含产品关键词
    title_has_prod = _has_any(title, _PROD_KEYWORDS_EN + _PROD_KEYWORDS_ZH)
    title_has_reg = _has_any(title, [
        "recall", "regulation", "directive", "standard", "law", "approval",
        "notice", "epr", "subsidy", "tariff", "compliance",
        "召回", "法规", "标准", "指令", "强制",
    ])
    if not (title_has_prod and title_has_reg):
        return ("", [])

    # 模拟 grounded fetch 拼出来的内容
    synth = (
        f"Synthesized regulatory summary for: {title}\n\n"
        f"Scope: applies to manufacturers, importers, and distributors of LEV products in {market}. "
        f"Article 1 - Definitions and applicability. "
        f"Article 2 - Conformity assessment by notified body before market placement. "
        f"Article 3 - Effective date and 18-month transition. "
        f"Article 4 - Penalties up to 4% of annual turnover. "
        f"Article 5 - Mandatory recall procedures and market surveillance. "
        f"Annex A: Technical parameters and test methods. "
        f"Annex B: Marking, labeling, and CE / type-plate requirements. "
        f"Annex C: Enforcement, market surveillance, judicial appeals."
    ) * 3
    return (synth, [])
