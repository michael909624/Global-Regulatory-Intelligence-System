"""
5000 条规模的"真实信息池"模拟器。

分布（按用户业务三级逻辑设计）：
  • IN_WINDOW_RELEVANT  =   30   时间窗内 + 业务范围内 → 应进周报
  • OUT_WINDOW_RELEVANT =  200   业务范围内但出时间窗 → LLM 应判"不相关/无新动作"，不进周报
  • DISTRACTOR          =  500   迷惑性强（标题含产品词但实际不沾）→ 不进周报
  • IRRELEVANT          = 4270   完全不相关 → 不进周报

标签语义（用于 LLM mock 行为分支 + 评测断言）：
  • SHOULD_APPEAR     真正应在周报里
  • OUT_OF_WINDOW     LLM 应识别为"过期/远期，无新合规义务" → 标"不相关"
  • DISTRACTOR        LLM 应识别为"标题像但实际不在业务范围" → 标"不相关"
  • IRRELEVANT        LLM 应直接判"不相关"

按真实分布近似：
  - 5000 条 ÷ 8 维 L3 ≈ 每维 600 条候选
  - 5 类产品分布按市场流量加权（ebike / e-scooter > 摩托 > 平衡车 > 割草机）
  - 市场覆盖：欧盟、德、法、英、美联邦、美州、中、日、韩、澳、加、意、西
  - 时间窗内：今日往前 90 天 + 今日起未来 365 天
  - 时间窗外：今日往前 5-15 年（已实施很久）/ 今日起 3-10 年后才生效
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

LabelKind = Literal["SHOULD_APPEAR", "OUT_OF_WINDOW", "DISTRACTOR", "IRRELEVANT"]


@dataclass
class PoolReg:
    id: str
    title: str
    market: str
    source_url: str
    reg_id: str | None
    snippet: str
    full_text: str
    publish_date: str         # YYYY-MM-DD（用于 time-window 判定）
    label: LabelKind          # 标准答案
    expected_products: list[str] = field(default_factory=list)
    expected_dimensions: list[str] = field(default_factory=list)
    expected_impact: str = "🟢"
    expected_markets: str = ""

    @property
    def should_appear_in_report(self) -> bool:
        return self.label == "SHOULD_APPEAR"


# ── 字典池 ────────────────────────────────────────────────────────────────────

_PRODUCTS_FULL = ["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"]

_MARKETS_DICT = [
    ("欧盟", "EU"),
    ("德国", "Germany"),
    ("法国", "France"),
    ("英国", "UK"),
    ("意大利", "Italy"),
    ("西班牙", "Spain"),
    ("荷兰", "Netherlands"),
    ("美国（联邦）", "US Federal"),
    ("美国（加州）", "California"),
    ("美国（纽约州）", "New York"),
    ("中国", "China"),
    ("日本", "Japan"),
    ("韩国", "Korea"),
    ("澳大利亚", "Australia"),
    ("加拿大", "Canada"),
]

_DIMS_ALL = ["RD", "PROD", "CERT", "IMPORT", "RETAIL", "USE", "ENFORCE", "EOL"]


# ── 合规 / 业务相关法规模板 ──────────────────────────────────────────────────

_RELEVANT_TEMPLATES = [
    # (title 模板, dim, products idx, impact)
    ("{market_en} Battery Safety Standard {std_id} for {prod_en}",
     ["RD", "CERT"], "battery", "🔴"),
    ("{market_en} EPR Regulation on Used Lithium Batteries from {prod_en}",
     ["EOL"], "battery", "🟡"),
    ("{market_en} Mandatory Helmet Law for {prod_en} Riders",
     ["USE"], "single", "🟡"),
    ("{market_en} Type Approval Requirements for {prod_en} Sold After {year}",
     ["CERT"], "single", "🔴"),
    ("{market_en} Safety Recall: {prod_en} Brake Defect — Notice {std_id}",
     ["ENFORCE"], "single", "🔴"),
    ("{market_en} Tariff Adjustment on Imported {prod_en} ({std_id})",
     ["IMPORT"], "single", "🟡"),
    ("{market_en} Subsidy Scheme for {prod_en} Purchases ({year})",
     ["RETAIL"], "single", "🟡"),
    ("{market_en} Cybersecurity Compliance for Connected {prod_en}",
     ["RD", "CERT"], "single", "🔴"),
    ("{market_en} REACH SVHC Update — Materials in {prod_en} Components",
     ["PROD"], "battery", "🟡"),
    ("{market_en} Charger and Battery Pack Standard {std_id} for LEV Devices",
     ["RD", "CERT"], "battery", "🔴"),
    ("{market_en} Sharing Operations License for {prod_en} ({year})",
     ["USE", "RETAIL"], "single", "🟡"),
    ("{market_en} Data Protection Compliance for {prod_en} Telematics",
     ["RD"], "single", "🟡"),
    # —— 边界 case：真 🟢（早期咨询 / 低紧迫但属业务范围）——
    # 验证 reporter 低置信过滤"🟢 AND dims=[]" 不会误伤"🟢 + dims 非空"的真相关条目
    ("{market_en} Public Consultation: {prod_en} Noise Limit Discussion Paper",
     ["RD"], "single", "🟢"),
    ("{market_en} Long-Term Roadmap Discussion: Future {prod_en} Standards",
     ["RD", "CERT"], "single", "🟢"),
]


# ── 迷惑性 distractor 模板 ──────────────────────────────────────────────────
# 标题里有 LEV 关键词，但内容明确不属于业务范围（建筑/船舶/工业/医疗等）

_DISTRACTOR_TEMPLATES = [
    ("{market_en} Fire Safety Code for Indoor {prod_en} Storage in Commercial Buildings",
     "fire-safety code applies only to building storage facilities, parking garages, and "
     "commercial premises. Does not regulate vehicle design, sale, or use. "
     "Building owners are responsible for fire suppression equipment."),
    ("{market_en} Industrial Forklift Variant Based on {prod_en} Frame Design",
     "industrial logistics forklift specification using a chassis derived from bicycle frame design. "
     "Applies exclusively to warehouse and factory logistics equipment. Not for road use, "
     "not for personal mobility products."),
    ("{market_en} Vessel-Mounted {prod_en} for Marine Tourism Applications",
     "specifications for vessel-mounted personal mobility devices used on cruise ships and "
     "ferries. Applies only to maritime tourism operators. Vessel safety regulations apply."),
    ("{market_en} Medical Device Adaptation: Modified {prod_en} for Hospital Mobility",
     "medical device classification of modified personal mobility products used by hospital "
     "patients. Subject to medical device directive, not consumer product law. "
     "Manufacturer must hold medical CE marking."),
    ("{market_en} Theatre Stage Prop Specification — {prod_en} Replicas for Performance",
     "specifications for stage prop replicas used in theatrical productions. "
     "Non-functional decorative items, not subject to vehicle or consumer electronics regulation."),
    ("{market_en} Educational Display Equipment — Static {prod_en} Models for Schools",
     "static educational display models for school technology classrooms. "
     "Non-operational teaching aids only, not consumer products."),
    ("{market_en} Military Reconnaissance Vehicle Variant of {prod_en}",
     "military classification — armored reconnaissance variant for defence procurement. "
     "Subject to ITAR / EU dual-use export controls only. Not a consumer or commercial product."),
    ("{market_en} Construction Site Tool: Heavy-Duty {prod_en} Modification",
     "heavy construction site equipment derived from {prod_en} chassis. Industrial machinery "
     "directive applies. Not consumer goods. Sold only to certified contractors."),
    ("{market_en} Aviation Ground Handling: {prod_en} for Airport Apron Use",
     "airport apron ground-support equipment. Subject to aviation authority approval and "
     "airport-specific safety protocols. Not a consumer product."),
    ("{market_en} Mining Operation: Underground {prod_en} for Tunnel Inspection",
     "underground mining tunnel inspection equipment specification. ATEX explosive atmosphere "
     "compliance required. Subject to mining industry regulation."),
]


# ── 完全不相关模板 ────────────────────────────────────────────────────────────

_IRRELEVANT_TEMPLATES = [
    ("{market_en} Pharmaceutical GMP Update for {topic}",
     "Pharmaceutical Good Manufacturing Practice — applies to medicinal products manufacturing. "
     "Active pharmaceutical ingredients, dosage forms, sterile production. "
     "No relation to consumer mobility devices or outdoor power equipment."),
    ("{market_en} Food Safety Regulation: {topic} in Fresh Produce",
     "Agricultural produce safety standards. Farm-to-table traceability requirements. "
     "Pesticide residue limits."),
    ("{market_en} Construction Material Standard — {topic} for Buildings",
     "Building material specification for residential and commercial construction. "
     "Structural integrity, fire resistance, thermal insulation requirements."),
    ("{market_en} Marine Engine Emission Tier — {topic} for Vessels Above 100 GT",
     "International Maritime Organization tier emissions for marine diesel engines. "
     "Applies only to commercial shipping above gross tonnage threshold."),
    ("{market_en} Aviation Authority Notice — {topic} for Commercial Aircraft",
     "Aviation safety directive for commercial aircraft maintenance and operations. "
     "Type-certified aircraft only. Pilot licensing requirements."),
    ("{market_en} Telecommunications Spectrum Allocation: {topic}",
     "Radio frequency spectrum allocation for mobile network operators. "
     "Base station licensing, interference mitigation."),
    ("{market_en} Banking Capital Requirements: {topic} Reform",
     "Basel III banking regulation update. Tier 1 capital ratio requirements. "
     "Risk-weighted asset calculations."),
    ("{market_en} Tobacco Products Directive — {topic} Labeling",
     "Tobacco product packaging and labeling directive. Health warning requirements."),
    ("{market_en} Cosmetics Safety Regulation — {topic} Ingredient Restrictions",
     "Cosmetic product safety regulation. Banned and restricted ingredient lists."),
    ("{market_en} Mining Operation License — {topic} Extraction Permit",
     "Mining sector environmental and operational license. Land restoration obligations."),
    ("{market_en} Agricultural Pesticide Approval — {topic} Active Substance",
     "Plant protection product approval. Toxicology assessment, environmental fate."),
    ("{market_en} Textile Labeling: {topic} Fiber Content Disclosure",
     "Textile composition labeling regulation. Fiber content percentage on garment labels."),
    ("{market_en} Dental Equipment Safety: {topic} Sterilization Standard",
     "Dental clinic equipment sterilization protocol. Autoclave performance requirements."),
    ("{market_en} Real Estate Disclosure: {topic} Property Transaction",
     "Real estate sale disclosure obligations. Energy performance certificate, structural surveys."),
    ("{market_en} Railway Signaling Standard — {topic} ETCS Implementation",
     "Railway European Train Control System signaling standard. "
     "Track-side beacons, on-board computers."),
]

_IRRELEVANT_TOPICS = [
    "Insulin Production", "Wheat Quality", "Steel Beams", "Container Ships", "Boeing 737",
    "5G NR", "Risk-Weighted Assets", "Health Warnings", "Parabens", "Copper Mining",
    "Glyphosate", "Wool", "Autoclaves", "EPC Certificates", "ETCS Level 3",
    "Cement", "Crude Oil", "Fishing Quotas", "Tax Returns", "Building Permits",
    "Sodium Chloride", "Sterile Bandages", "Wood Pulp", "Diamond Imports", "Vaccines",
]


# ── 生成器 ────────────────────────────────────────────────────────────────────


def _hash_id(parts: tuple) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def _date_in_window(rng: random.Random, today: datetime) -> str:
    """近 90 天内（新发布）或未来 365 天内（即将生效）。"""
    if rng.random() < 0.6:
        days = rng.randint(0, 90)
        d = today - timedelta(days=days)
    else:
        days = rng.randint(0, 365)
        d = today + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _date_out_window(rng: random.Random, today: datetime) -> str:
    """已实施 >2 年的旧法规 / 远期 >2 年后才生效的草案。"""
    if rng.random() < 0.7:
        days = rng.randint(730, 365 * 15)
        d = today - timedelta(days=days)
    else:
        days = rng.randint(730, 365 * 10)
        d = today + timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _pick_products(rng: random.Random, kind: str) -> list[str]:
    """battery=多产品；single=随机 1-2 个产品；all=全部 5 类。"""
    if kind == "battery":
        # 电池规则通常覆盖 4 类（除非明确割草机条款）
        if rng.random() < 0.3:
            return list(_PRODUCTS_FULL)
        return [p for p in _PRODUCTS_FULL if p != "智能割草机"]
    if kind == "all":
        return list(_PRODUCTS_FULL)
    if kind == "single":
        n = rng.choice([1, 1, 1, 2])
        return rng.sample(_PRODUCTS_FULL, k=min(n, 5))
    return [rng.choice(_PRODUCTS_FULL)]


def _gen_in_window_relevant(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    """30 条：相关 + 时间窗内"""
    out = []
    for i in range(n):
        title_tpl, dims, prod_kind, impact = rng.choice(_RELEVANT_TEMPLATES)
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        prod_zh = rng.choice(_PRODUCTS_FULL)
        prod_en_map = {
            "电助力自行车": "E-Bikes",
            "电动滑板车": "E-Scooters",
            "电动平衡车": "Hoverboards",
            "电动摩托车": "Electric Motorcycles",
            "智能割草机": "Robotic Lawn Mowers",
        }
        prod_en = prod_en_map[prod_zh]
        std_id = f"{rng.choice(['EN','UL','IEC','GB','UN R'])}-{rng.randint(50, 99999)}"
        year = today.year + rng.choice([-1, 0, 1])
        title = title_tpl.format(market_en=market_en, prod_en=prod_en, std_id=std_id, year=year)
        publish_date = _date_in_window(rng, today)

        full_text = (
            f"{title}\n\n"
            f"This {market_en} regulation establishes requirements for {prod_en}. "
            f"Effective date: {publish_date}. Reference: {std_id}. "
            f"Applies to manufacturers, importers, and distributors placing {prod_en} on the {market_en} market. "
            f"Conformity assessment by notified body. Penalties up to 4% of annual turnover. "
            "Articles I-III specify scope and definitions. Articles IV-VI specify technical requirements. "
            "Article VII specifies enforcement. Annex A: test methods. Annex B: marking requirements. "
            "Member states must transpose by the effective date."
        ) * 2

        out.append(PoolReg(
            id=f"IW_{i:03d}_{_hash_id((market_zh, prod_zh, std_id))}",
            title=title,
            market=market_zh,
            source_url=f"https://example.gov/{market_en.lower().replace(' ', '-')}/reg/{std_id}/{i}",
            reg_id=std_id,
            snippet=f"{market_zh} {prod_zh} 法规：{title_tpl[:30]}",
            full_text=full_text,
            publish_date=publish_date,
            label="SHOULD_APPEAR",
            expected_products=_pick_products(rng, prod_kind),
            expected_dimensions=list(dims),
            expected_impact=impact,
            expected_markets=market_zh,
        ))
    return out


def _gen_out_window_relevant(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    """200 条：相关业务范围但超出时间窗（旧法规 / 远期草案）"""
    out = []
    for i in range(n):
        title_tpl, dims, prod_kind, _ = rng.choice(_RELEVANT_TEMPLATES)
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        prod_zh = rng.choice(_PRODUCTS_FULL)
        prod_en_map = {
            "电助力自行车": "E-Bikes", "电动滑板车": "E-Scooters",
            "电动平衡车": "Hoverboards", "电动摩托车": "Electric Motorcycles",
            "智能割草机": "Robotic Lawn Mowers",
        }
        prod_en = prod_en_map[prod_zh]
        std_id = f"{rng.choice(['EN','UL','IEC','GB','UN R'])}-{rng.randint(100, 99999)}"
        year = today.year + rng.choice([-10, -8, -6, -4, 4, 6, 8])
        title = title_tpl.format(market_en=market_en, prod_en=prod_en, std_id=std_id, year=year)
        publish_date = _date_out_window(rng, today)

        # 标注：原文带"已实施多年"或"远期生效"提示
        if datetime.fromisoformat(publish_date) < today:
            era = "Originally enacted in {y}; has been in effect for over {n} years with no recent amendments.".format(
                y=publish_date[:4], n=today.year - int(publish_date[:4]),
            )
        else:
            era = "Long-term draft scheduled for effect in {y}; no immediate compliance action required at present.".format(y=publish_date[:4])

        full_text = (
            f"{title}\n\n"
            f"{era} The text below is for historical reference. "
            f"This regulation applied to {prod_en} in {market_en}. "
            f"Reference: {std_id}. Subsequent amendments have superseded portions of this text."
        ) * 4

        out.append(PoolReg(
            id=f"OW_{i:03d}_{_hash_id((market_zh, prod_zh, std_id, i))}",
            title=title,
            market=market_zh,
            source_url=f"https://archive.example.gov/{market_en.lower().replace(' ', '-')}/legacy/{std_id}/{i}",
            reg_id=std_id,
            snippet=f"{era[:80]}",
            full_text=full_text,
            publish_date=publish_date,
            label="OUT_OF_WINDOW",
            expected_products=_pick_products(rng, prod_kind),
            expected_dimensions=[],
            expected_impact="🟢",
            expected_markets="",
        ))
    return out


def _gen_distractors(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    """500 条：标题含产品关键词但实际不相关（建筑/船舶/医疗/工业等）"""
    out = []
    for i in range(n):
        title_tpl, body = rng.choice(_DISTRACTOR_TEMPLATES)
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        prod_zh = rng.choice(_PRODUCTS_FULL)
        prod_en_map = {
            "电助力自行车": "Bicycle Frames", "电动滑板车": "Personal Mobility",
            "电动平衡车": "Self-Balancing Devices", "电动摩托车": "Two-Wheel Vehicles",
            "智能割草机": "Lawn Mower",
        }
        prod_en = prod_en_map[prod_zh]
        title = title_tpl.format(market_en=market_en, prod_en=prod_en)
        publish_date = _date_in_window(rng, today)

        full_text = (
            f"{title}\n\n"
            f"{body.format(prod_en=prod_en)}\n"
            f"This regulation does not apply to consumer mobility devices or outdoor power equipment "
            f"sold to end users. Applicability strictly limited to the scope above. "
            f"Reference: {market_en}-IND-{rng.randint(10000, 99999)}."
        ) * 3

        out.append(PoolReg(
            id=f"DT_{i:03d}_{_hash_id((market_zh, title_tpl, i))}",
            title=title,
            market=market_zh,
            source_url=f"https://industrial.example.gov/{market_en.lower().replace(' ', '-')}/{i}",
            reg_id=f"{market_en}-IND-{rng.randint(10000, 99999)}",
            snippet=title_tpl[:60],
            full_text=full_text,
            publish_date=publish_date,
            label="DISTRACTOR",
            expected_products=[],   # LLM 应判"不相关"
            expected_dimensions=[],
            expected_impact="🟢",
            expected_markets="",
        ))
    return out


def _gen_irrelevant(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    """4270 条：完全不相关（医药/食品/建筑/航运等）"""
    out = []
    for i in range(n):
        title_tpl, body = rng.choice(_IRRELEVANT_TEMPLATES)
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        topic = rng.choice(_IRRELEVANT_TOPICS)
        title = title_tpl.format(market_en=market_en, topic=topic)
        publish_date = _date_in_window(rng, today)

        full_text = (
            f"{title}\n\n"
            f"{body}\n"
            f"Reference: {market_en}-{topic.replace(' ', '-')}-{rng.randint(1000, 99999)}.\n"
            "Scope: as defined above. Out of scope: any consumer product not in the listed industry. "
            "Outdoor power equipment, light electric vehicles, personal mobility — not covered."
        ) * 2

        out.append(PoolReg(
            id=f"IR_{i:04d}_{_hash_id((market_zh, topic, i))}",
            title=title,
            market=market_zh,
            source_url=f"https://other.example.gov/{topic.lower().replace(' ', '-')}/{i}",
            reg_id=f"{market_en}-{topic[:6]}-{rng.randint(1000, 99999)}",
            snippet=title_tpl[:50],
            full_text=full_text,
            publish_date=publish_date,
            label="IRRELEVANT",
            expected_products=[],
            expected_dimensions=[],
            expected_impact="🟢",
            expected_markets="",
        ))
    return out


def _dedupe_titles(pool: list[PoolReg]) -> list[PoolReg]:
    """保证 title 全局唯一——模板组合空间有限，撞名时追加 (#NNN) 区分号。

    不能让重复 title 进 pool，因为：
      • raw_search_results.content_hash UNIQUE = reg_hash(title) 会去重，
        多条同 title 的 case 共用一行 raw → 评测时无法区分谁是谁。
    """
    seen: set[str] = set()
    out: list[PoolReg] = []
    for p in pool:
        key = p.title.strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
            continue
        # 撞名 → 追加序号到 title
        for n in range(1, 10000):
            new_title = f"{p.title} (#{n:04d})"
            new_key = new_title.strip().lower()
            if new_key not in seen:
                seen.add(new_key)
                p.title = new_title
                out.append(p)
                break
    return out


def generate_pool(
    n_in_window: int = 30,
    n_out_window: int = 200,
    n_distractor: int = 500,
    n_irrelevant: int = 4270,
    seed: int = 42,
    today: datetime | None = None,
) -> list[PoolReg]:
    rng = random.Random(seed)
    today = today or datetime.now()

    pool = (
        _gen_in_window_relevant(n_in_window, rng, today)
        + _gen_out_window_relevant(n_out_window, rng, today)
        + _gen_distractors(n_distractor, rng, today)
        + _gen_irrelevant(n_irrelevant, rng, today)
    )
    pool = _dedupe_titles(pool)
    rng.shuffle(pool)
    return pool


if __name__ == "__main__":
    pool = generate_pool()
    from collections import Counter
    label_counts = Counter(p.label for p in pool)
    print(f"Total: {len(pool)}")
    for k, v in label_counts.items():
        print(f"  {k:<18}: {v}")
    print()
    print("样本：")
    for p in pool[:5]:
        print(f"  [{p.label}] {p.title[:80]}")
