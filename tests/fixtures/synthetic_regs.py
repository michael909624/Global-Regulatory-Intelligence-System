"""
端到端召回评测的虚拟法规集。

每条 SyntheticReg 模拟一条 researcher 召回 + scraper 抓取后的完整数据：
  - title / source_url / market / reg_id / snippet / full_text
  - 人工标注的"标准答案"：should_appear_in_report、expected_products、
    expected_dimensions、expected_impact、expected_markets

设计原则（覆盖三级业务逻辑 = business_scope L1/L2/L3）：
  • L1：5 类整机（电动滑板车 / 平衡车 / ebike / 电摩 / 智能割草机）
        每类至少 3 条命中 + 1 条边缘
  • L2：售前 / 售中 / 售后
  • L3：8 维（RD / PROD / CERT / IMPORT / RETAIL / USE / ENFORCE / EOL）
        每维至少 2 条覆盖

反例（应被判"不相关"）：
  • 四轮乘用车 / 商用车
  • 医疗器械 / 手术设备
  • 船舶 / 航空 / 卫星
  • 数据中心 / 云服务
  • 卫浴 / 厨电 / 家具

边缘（容易出错）：
  • 多产品（覆盖整个微出行品类）
  • 多市场（跨欧美亚）
  • 同一法规多条入库（reg_id 异写）
  • 极短文本（导航页）
  • 合成内容（[Gemini synthesis]）
  • 标题中文 + 原文英文
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SyntheticReg:
    id: str                           # 测试 ID（不入库，只用于断言定位）
    title: str
    market: str                       # researcher 阶段填的 market_hint
    source_url: str
    reg_id: str | None = None
    snippet: str = ""                 # researcher 的 relevance_note
    full_text: str = ""               # scraper 的 sc.full_text

    # 人工标注的"标准答案"
    should_appear_in_report: bool = True
    expected_products: list[str] = field(default_factory=list)   # 5 类整机标准名
    expected_dimensions: list[str] = field(default_factory=list) # 八维 L3
    expected_impact: str = "🟡"                                  # 🔴 / 🟡(🟢 已废弃)
    expected_markets: str = ""

    # 故意制造的"陷阱"标记（用于 LLM 模拟器注入噪声）
    trap_kind: str = ""               # "list_products" / "empty_response" /
                                      # "contradictory_merge" / "safety_block" / ""


# ── 25 条正例 ────────────────────────────────────────────────────────────────
POSITIVE_CASES: list[SyntheticReg] = [
    # ── L1: 电助力自行车 + L3: RD/CERT ──
    SyntheticReg(
        id="P01_eu_battery_reg",
        title="Regulation (EU) 2023/1542 on batteries and waste batteries",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg/2023/1542/oj",
        reg_id="(EU) 2023/1542",
        snippet="电池碳足迹与回收义务，覆盖 LEV 电池组",
        full_text=(
            "Regulation (EU) 2023/1542 of the European Parliament and of the Council "
            "of 12 July 2023 concerning batteries and waste batteries. "
            "Applies to all batteries placed on the EU market including LMT (light means of transport) "
            "batteries used in e-bikes, e-scooters, electric mopeds. "
            "Requires carbon footprint declaration from 2025-08-18, "
            "battery passport from 2027-02-18, and EPR/take-back scheme. "
            "Article 7 carbon footprint declaration. Article 64 battery passport. "
            "Producers must register with national EPR scheme. "
            "Effective date: 2024-02-18. Enforcement of carbon footprint: 2025-08-18."
        ),
        expected_products=["电助力自行车", "电动摩托车", "电动滑板车", "电动平衡车"],
        expected_dimensions=["RD", "EOL", "CERT"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),
    SyntheticReg(
        id="P02_gb17761",
        title="GB 17761-2018 电动自行车安全技术规范",
        market="中国",
        source_url="https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=44E0C2FB31A3E55174C492F0A4D6D81F",
        reg_id="GB 17761-2018",
        snippet="中国电动自行车强制国标",
        full_text=(
            "GB 17761-2018 电动自行车安全技术规范。本标准规定了电动自行车的整车性能、"
            "电气安全、机械安全等要求。最高时速不超过 25 km/h，整车质量不大于 55 kg。"
            "强制性国家标准，自 2019 年 4 月 15 日起实施。"
            "适用于在中国境内销售的所有电动自行车产品。"
            "生产、销售不符合本标准的产品将被市场监管部门查处。"
        ),
        expected_products=["电助力自行车"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="中国",
    ),
    SyntheticReg(
        id="P03_un_r136",
        title="UN Regulation No. 136 on electric vehicle category L safety",
        market="欧盟",
        source_url="https://unece.org/transport/documents/2021/06/standards/un-regulation-no-136",
        reg_id="UN R136",
        snippet="L 类电动车安全（电池防火）",
        full_text=(
            "UN ECE Regulation No. 136 — Uniform provisions concerning the approval of "
            "vehicles of category L with regard to specific requirements for the electric powertrain. "
            "Applies to L1e through L7e categories — covering electric mopeds and motorcycles. "
            "Requires battery thermal runaway test, post-crash safety, and isolation resistance. "
            "Mandatory from 2024-09-01 for new type approvals."
        ),
        expected_products=["电动摩托车", "电助力自行车"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),

    # ── L1: 电动滑板车 + L3: USE ──
    SyntheticReg(
        id="P04_germany_ekfv",
        title="eKFV — Verordnung über die Teilnahme von Elektrokleinstfahrzeugen am Straßenverkehr",
        market="德国",
        source_url="https://www.gesetze-im-internet.de/ekfv/",
        reg_id="eKFV",
        snippet="德国电动滑板车上路法规",
        full_text=(
            "Elektrokleinstfahrzeuge-Verordnung (eKFV) regelt die Anforderungen "
            "für Elektrokleinstfahrzeuge im Straßenverkehr in Deutschland. "
            "Gilt für E-Scooter mit Höchstgeschwindigkeit zwischen 6 und 20 km/h. "
            "Anforderungen: Allgemeine Betriebserlaubnis, Versicherungspflicht "
            "(Versicherungskennzeichen), Mindestalter 14 Jahre. "
            "Inkrafttreten geändert: 2025-03-01. "
            "Verstöße werden mit Bußgeld geahndet."
        ),
        expected_products=["电动滑板车"],
        expected_dimensions=["USE"],
        expected_impact="🟡",
        expected_markets="德国",
    ),
    SyntheticReg(
        id="P05_france_share",
        title="Décret n° 2024-XXXX sur les trottinettes électriques en libre-service",
        market="法国",
        source_url="https://www.legifrance.gouv.fr/jorf/id/2024-XXXX",
        reg_id="Décret 2024-XXXX",
        snippet="法国共享电动滑板车运营许可",
        full_text=(
            "Décret relatif aux trottinettes électriques en libre-service. "
            "Les opérateurs de free-floating doivent obtenir une autorisation municipale, "
            "limiter la vitesse à 15 km/h en ville, et installer des zones de stationnement obligatoires. "
            "Application à compter du 1er juillet 2024. "
            "Sanctions : amende de 1 500 € par véhicule non conforme."
        ),
        expected_products=["电动滑板车"],
        expected_dimensions=["USE", "RETAIL"],
        expected_impact="🟡",
        expected_markets="法国",
    ),
    SyntheticReg(
        id="P06_uk_psti",
        title="Product Security and Telecommunications Infrastructure Act 2022",
        market="英国",
        source_url="https://www.legislation.gov.uk/ukpga/2022/46/contents",
        reg_id="PSTI",
        snippet="UK 物联网产品安全法",
        full_text=(
            "The Product Security and Telecommunications Infrastructure (PSTI) Act 2022 "
            "imposes minimum security requirements on consumer connectable products including "
            "smart locks, IoT devices, e-bikes with companion apps, robotic lawn mowers with "
            "Bluetooth/WiFi connectivity. "
            "Mandatory password security, vulnerability disclosure policy, and security update period. "
            "Effective: 29 April 2024. "
            "Enforcement by OPSS — fines up to £10 million or 4% of global turnover."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="英国",
    ),

    # ── L1: 智能割草机 + L3: RD/EOL ──
    SyntheticReg(
        id="P07_en_50636_robotic_mower",
        title="EN 50636-2-107 Safety of household and similar appliances — Robotic battery powered lawnmowers",
        market="欧盟",
        source_url="https://standards.cencenelec.eu/en/50636-2-107",
        reg_id="EN 50636-2-107",
        snippet="欧盟机器人割草机安全标准",
        full_text=(
            "EN 50636-2-107:2015+A2:2024 — Safety of household and similar appliances. "
            "Particular requirements for robotic battery powered electrical lawnmowers. "
            "Specifies obstacle detection, blade safety, child-resistance design, "
            "perimeter wire integrity, and emergency stop requirements. "
            "Mandatory for CE marking under Machinery Directive. "
            "Replaces older robotic mower safety baselines from 2024-12-31."
        ),
        expected_products=["智能割草机"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),
    SyntheticReg(
        id="P08_iec62133_battery_pack",
        title="IEC 62133-2:2017+AMD1:2021 Secondary cells and batteries — lithium",
        market="全球通用",
        source_url="https://webstore.iec.ch/publication/32662",
        reg_id="IEC 62133-2",
        snippet="锂电池组通用安全标准",
        full_text=(
            "IEC 62133-2 specifies safety requirements for secondary lithium cells and "
            "batteries used in portable applications. Tests include short circuit, "
            "abuse, vibration, mechanical shock, thermal shock. "
            "Applicable to all battery packs in portable electric devices including "
            "e-bikes, e-scooters, motorcycles, robotic lawn mowers, and consumer electronics. "
            "Mandatory for many national certification schemes (CB, KC, PSE)."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="全球通用",
    ),

    # ── L3: PROD（物质限制 / 供应链）──
    SyntheticReg(
        id="P09_eu_rohs_pfas",
        title="Commission Delegated Regulation (EU) 2024/XXX amending RoHS Annex II",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg_del/2024/XXX/oj",
        reg_id="(EU) 2024/XXX",
        snippet="RoHS 限制 PFAS 修订",
        full_text=(
            "Commission Delegated Regulation amending Annex II of Directive 2011/65/EU (RoHS) "
            "to restrict per- and polyfluoroalkyl substances (PFAS) in electrical and electronic equipment. "
            "Affects all consumer electronics including LEVs (e-bikes, e-scooters, hoverboards), "
            "garden equipment (robotic mowers), and battery packs. "
            "Transition period: 24 months from publication. "
            "Effective: 2026-01-15. Enforcement: 2028-01-15."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["PROD"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),
    SyntheticReg(
        id="P10_reach_svhc",
        title="REACH SVHC Candidate List Update — Q2 2026",
        market="欧盟",
        source_url="https://echa.europa.eu/candidate-list-table",
        reg_id="REACH",
        snippet="REACH 高关注物质清单更新",
        full_text=(
            "ECHA has added 5 new substances to the SVHC candidate list under REACH. "
            "Affected materials include certain phthalates used in cable insulation, "
            "lead compounds in solder, and specific UV stabilizers in plastic housings. "
            "Articles containing >0.1% w/w must notify SCIP database within 6 months. "
            "Suppliers of e-bike, e-scooter, motorcycle batteries and chargers, robotic mower "
            "housings must update their substance declarations."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["PROD"],
        expected_impact="🟡",
        expected_markets="欧盟",
    ),

    # ── L3: IMPORT（进口/海关/危险品）──
    SyntheticReg(
        id="P11_un38_3_revision",
        title="UN 38.3 Revision — Lithium Battery Transport Test Requirements 2026",
        market="全球通用",
        source_url="https://unece.org/transport/dangerous-goods/un383-revision",
        reg_id="UN 38.3",
        snippet="锂电池运输测试规定修订",
        full_text=(
            "UN Manual of Tests and Criteria, Section 38.3 has been revised. "
            "New requirements for thermal stability test on lithium battery packs >100 Wh. "
            "Affects shipping of e-bike batteries (typically 400-700 Wh), e-scooter batteries "
            "(200-500 Wh), electric motorcycle batteries, and robotic lawn mower battery packs. "
            "Air, sea, and road transport. "
            "Effective: 2026-01-01."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["PROD", "IMPORT"],
        expected_impact="🔴",
        expected_markets="全球通用",
    ),
    SyntheticReg(
        id="P12_us_section301_china",
        title="USTR Section 301 Tariff Increase on Chinese E-Bikes and Lithium Batteries",
        market="美国",
        source_url="https://ustr.gov/about-us/policy-offices/press-office/press-releases/2024/section301",
        reg_id="USTR 301-2024",
        snippet="美国对华电动自行车关税上调",
        full_text=(
            "USTR Section 301 final action raises tariffs on Chinese imports: "
            "Lithium-ion batteries from 7.5% to 25% effective 2025-01-01; "
            "Electric bicycles HS 8711.60 from 0% to 20%; "
            "Critical minerals processed in China subject to additional 25% by 2026."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车"],
        expected_dimensions=["IMPORT"],
        expected_impact="🔴",
        expected_markets="美国（联邦）",
    ),

    # ── L3: RETAIL（销售/补贴/广告）──
    SyntheticReg(
        id="P13_italy_ebike_subsidy",
        title="Decreto MASE — Incentivi per acquisto biciclette elettriche 2026",
        market="意大利",
        source_url="https://www.mase.gov.it/incentivi-bici-elettriche-2026",
        reg_id="DM 2026/MASE",
        snippet="意大利电助力自行车补贴",
        full_text=(
            "Decreto del Ministero dell'Ambiente recante incentivi all'acquisto di "
            "biciclette a pedalata assistita (EPAC) e cargo bike. "
            "Bonus fino a 750 € per acquisto di EPAC e 1500 € per cargo bike. "
            "Periodo di applicazione: 1 marzo 2026 — 31 dicembre 2026. "
            "Beneficiari: persone fisiche residenti in Italia."
        ),
        expected_products=["电助力自行车"],
        expected_dimensions=["RETAIL"],
        expected_impact="🟡",
        expected_markets="意大利",
    ),
    SyntheticReg(
        id="P14_nyc_ebike_battery_law",
        title="NYC Local Law 39 — Powered Mobility Device Battery Sales Restrictions",
        market="美国",
        source_url="https://www.nyc.gov/site/fdny/codes/local-law-39",
        reg_id="NYC LL 39",
        snippet="纽约市电池销售禁令（仅限认证产品）",
        full_text=(
            "NYC Local Law 39 prohibits the sale, lease, or rental of "
            "powered mobility devices (e-bikes, e-scooters, hoverboards) and their "
            "lithium-ion batteries unless certified to UL 2849 (e-bikes), UL 2272 (hoverboards/e-scooters), "
            "or UL 2271 (battery packs). "
            "Effective: 2023-09-16. Civil penalty up to $1,000 per violation."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车"],
        expected_dimensions=["CERT", "RETAIL"],
        expected_impact="🔴",
        expected_markets="美国（纽约州）",
    ),

    # ── L3: ENFORCE（召回/罚款）──
    SyntheticReg(
        id="P15_cpsc_recall_hoverboard",
        title="CPSC Recall — Hoverboard Battery Fire Hazard",
        market="美国",
        source_url="https://www.cpsc.gov/Recalls/2026/Hoverboard-Battery-Fire",
        reg_id="CPSC-2026-RC-XXX",
        snippet="美国 CPSC 平衡车电池起火召回",
        full_text=(
            "U.S. Consumer Product Safety Commission announces a recall of approximately "
            "120,000 self-balancing scooters (hoverboards) due to lithium-ion battery "
            "fire and explosion hazard. "
            "Consumers should immediately stop use and contact the manufacturer for free repair. "
            "Product: Brand X Hoverboard Model HB-2024. "
            "Sold from January 2024 through December 2025 at major retailers."
        ),
        expected_products=["电动平衡车"],
        expected_dimensions=["ENFORCE"],
        expected_impact="🔴",
        expected_markets="美国（联邦）",
    ),
    SyntheticReg(
        id="P16_eu_safety_gate_escooter",
        title="EU Safety Gate Alert A12/2026 — Defective E-Scooter Brakes",
        market="欧盟",
        source_url="https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/A12-2026",
        reg_id="A12/2026",
        snippet="欧盟快速预警：电动滑板车制动缺陷",
        full_text=(
            "EU Safety Gate weekly alert A12/2026 — Defective braking system on certain "
            "e-scooter models imported from third countries. "
            "Risk: serious injury due to brake failure at speed. "
            "Measures: withdrawal from the market, recall from end users. "
            "Distribution: Spain, France, Germany, Italy. "
            "Notifying country: Spain. Date of notification: 2026-04-15."
        ),
        expected_products=["电动滑板车"],
        expected_dimensions=["ENFORCE"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),

    # ── L3: EOL（回收/处置）──
    SyntheticReg(
        id="P17_germany_battg",
        title="BattG-DV — Verordnung über die Verwertung von Industriebatterien",
        market="德国",
        source_url="https://www.gesetze-im-internet.de/battg-dv/",
        reg_id="BattG-DV",
        snippet="德国电池回收义务实施细则",
        full_text=(
            "Verordnung zur Durchführung des Batteriegesetzes — präzisiert Rücknahme- "
            "und Verwertungspflichten für Industriebatterien einschließlich LEV-Batterien "
            "(E-Bikes, E-Scooter, E-Motorräder). "
            "Hersteller müssen sich beim Stiftung EAR registrieren und Mindestrücknahmequoten erfüllen. "
            "Inkrafttreten: 2026-08-18 (synchron mit EU-Batterieverordnung)."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车"],
        expected_dimensions=["EOL"],
        expected_impact="🔴",
        expected_markets="德国",
    ),
    SyntheticReg(
        id="P18_basel_convention_battery",
        title="Basel Convention BC-15/18 Decision on Transboundary Movement of Waste Batteries",
        market="全球通用",
        source_url="https://www.basel.int/Implementation/TechnicalAssistance/BC-15-18",
        reg_id="BC-15/18",
        snippet="巴塞尔公约废电池跨境转移",
        full_text=(
            "Decision BC-15/18 of the Conference of the Parties to the Basel Convention "
            "on transboundary movements of hazardous waste batteries. "
            "Applies to spent lithium-ion batteries from e-mobility products. "
            "Requires prior informed consent for transboundary shipments and "
            "approved environmentally sound management facilities at destination. "
            "National implementation: by 2027-12-31."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["EOL"],
        expected_impact="🟡",
        expected_markets="全球通用",
    ),

    # ── 跨语言 / 跨国家 ──
    SyntheticReg(
        id="P19_japan_pse_ebike",
        title="電気用品安全法（PSE）改正：電動アシスト自転車のリチウムイオン蓄電池",
        market="日本",
        source_url="https://www.meti.go.jp/policy/consumer/seian/denan/pse2026.html",
        reg_id="PSE 2026 改正",
        snippet="日本 PSE 法修订：电助力自行车锂电池",
        full_text=(
            "電気用品安全法施行規則の一部を改正する省令により、電動アシスト自転車用の "
            "リチウムイオン蓄電池が PSE「特定電気用品」（菱形 PSE）に追加された。 "
            "対象範囲：電動アシスト自転車および電動車椅子用の充電式リチウムイオン電池。 "
            "施行：2027 年 4 月 1 日。経過措置：1 年間。 "
            "経済産業省への事業届出と適合性検査が必要。"
        ),
        expected_products=["电助力自行车"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="日本",
    ),
    SyntheticReg(
        id="P20_korea_kc_escooter",
        title="전동킥보드 안전기준 KC 인증 강화 (산업통상자원부 고시 2026-XX호)",
        market="韩国",
        source_url="https://www.motie.go.kr/notice/2026-XX",
        reg_id="KC 2026-XX",
        snippet="韩国 KC 认证强化电动滑板车安全",
        full_text=(
            "산업통상자원부 고시 제2026-XX호 전동킥보드 안전기준 강화에 관한 고시. "
            "전동킥보드 및 전동 호버보드에 대한 KC 인증 요건 강화. "
            "리튬이온 배터리 안전, 제동 성능, EMC 시험 추가. "
            "시행일: 2026년 9월 1일. "
            "위반 시 국가기술표준원에 의해 시정명령 또는 과태료 부과."
        ),
        expected_products=["电动滑板车", "电动平衡车"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="韩国",
    ),
    SyntheticReg(
        id="P21_australia_accc_ebike",
        title="ACCC Mandatory Safety Standard — E-Bike Batteries and Chargers",
        market="澳大利亚",
        source_url="https://www.productsafety.gov.au/standards/e-bike-batteries-2026",
        reg_id="ACCC 2026 PS",
        snippet="澳大利亚 ACCC e-bike 电池强制标准",
        full_text=(
            "ACCC mandatory safety standard for e-bike, e-scooter and motorcycle "
            "lithium-ion battery packs and their dedicated chargers. "
            "Aligned with UL 2271 / UL 2849 / IEC 62133-2 testing. "
            "Compulsory third-party certification before sale. "
            "Effective: 2026-12-01. Penalties: up to AUD 50 million per breach (corporate)."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车"],
        expected_dimensions=["CERT", "RD"],
        expected_impact="🔴",
        expected_markets="澳大利亚",
    ),

    # ── 草案 / 早期阶段（统一 🟡;旧三档时代会标 🟢,现已废弃）──
    SyntheticReg(
        id="P22_eu_dpp_consultation",
        title="EU Digital Product Passport — Public Consultation Q3 2026",
        market="欧盟",
        source_url="https://ec.europa.eu/consultation/dpp-2026",
        reg_id="DPP 2026 Consultation",
        snippet="欧盟数字产品护照公示",
        full_text=(
            "European Commission opens public consultation on the implementation framework "
            "of Digital Product Passport (DPP) for batteries, electronics, and consumer goods. "
            "Will affect e-bikes, e-scooters, electric motorcycles, robotic lawn mowers, "
            "and their battery packs. "
            "Consultation period: 2026-07-01 to 2026-09-30. "
            "Implementing acts expected 2027-Q2."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["PROD", "CERT"],
        expected_impact="🟡",
        expected_markets="欧盟",
    ),
    SyntheticReg(
        id="P23_california_ebike_helmet",
        title="California AB-XXXX — Mandatory Helmet for E-Bike Class 3 Riders",
        market="美国",
        source_url="https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=2026AB-XXXX",
        reg_id="CA AB-XXXX",
        snippet="加州 Class 3 电助力自行车强制头盔法案",
        full_text=(
            "California Assembly Bill XXXX would require all riders of Class 3 e-bikes "
            "(speed pedelecs up to 28 mph) to wear a bicycle helmet, regardless of age. "
            "Bill currently in committee — first hearing scheduled 2026-05-15. "
            "If passed, effective 2027-01-01. "
            "Civil penalty: $25 per violation."
        ),
        expected_products=["电助力自行车"],
        expected_dimensions=["USE"],
        expected_impact="🟡",  # 跨州 + 知名议题 → 🟡(旧三档下会犹豫到 🟢,现统一 🟡)
        expected_markets="美国（加州）",
    ),
    SyntheticReg(
        id="P24_china_3c_battery",
        title="GB/T 36972-2018 电动自行车用锂离子蓄电池",
        market="中国",
        source_url="https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=GB-36972",
        reg_id="GB/T 36972-2018",
        snippet="中国电动自行车锂电池标准",
        full_text=(
            "GB/T 36972-2018 电动自行车用锂离子蓄电池技术规范。"
            "本标准规定了电动自行车用锂离子蓄电池的技术要求、试验方法、检验规则、"
            "标志、包装、运输和贮存。 适用于公称电压不大于 60V，额定容量不大于 30Ah 的电池组。"
            "推荐性国家标准。 实施日期：2019 年 1 月 1 日。"
        ),
        expected_products=["电助力自行车"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🟡",  # GB/T 推荐性而非强制，C2 + 阶段 4
        expected_markets="中国",
    ),

    # ── 跨产品 + 维度复合 ──
    SyntheticReg(
        id="P25_cra_2024_2847",
        title="Regulation (EU) 2024/2847 — Cyber Resilience Act (CRA)",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg/2024/2847/oj",
        reg_id="(EU) 2024/2847",
        snippet="欧盟网络韧性法案",
        full_text=(
            "Regulation (EU) 2024/2847 of the European Parliament and of the Council "
            "on horizontal cybersecurity requirements for products with digital elements (CRA). "
            "Applies to all connected products including e-bikes with companion apps, "
            "e-scooters with IoT features, robotic lawn mowers with WiFi/Bluetooth, "
            "and any battery management system with wireless capabilities. "
            "Mandatory CE marking for cybersecurity. Conformity assessment required. "
            "Effective: 2024-12-10. Full application: 2027-12-11."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),
]


# ── 15 条反例（应被判"不相关"，不进周报）────────────────────────────────────
NEGATIVE_CASES: list[SyntheticReg] = [
    SyntheticReg(
        id="N01_passenger_car_recall",
        title="NHTSA Recall — Toyota Camry Brake System",
        market="美国",
        source_url="https://www.nhtsa.gov/recalls/camry-brakes-2026",
        reg_id="NHTSA 26V-XXX",
        snippet="丰田凯美瑞制动系统召回",
        full_text=(
            "National Highway Traffic Safety Administration recalls 250,000 Toyota Camry "
            "vehicles model years 2022-2024 due to defective brake master cylinder. "
            "M1 category passenger cars only. Not applicable to motorcycles, mopeds, or "
            "any L-category vehicles. Dealers will replace the master cylinder free of charge."
        ),
        should_appear_in_report=False,
        expected_products=[],
        expected_dimensions=[],
        expected_markets="",
    ),
    SyntheticReg(
        id="N02_medical_device",
        title="FDA Class III Medical Device — Surgical Robot Approval",
        market="美国",
        source_url="https://www.fda.gov/medical-devices/surgical-robots",
        reg_id="FDA-510K-XXX",
        snippet="FDA 手术机器人批准",
        full_text=(
            "FDA grants 510(k) clearance for Da Vinci Xi surgical robot software update. "
            "Device intended for laparoscopic surgical procedures only. "
            "Not applicable to consumer electronics or personal mobility devices."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N03_data_center_cooling",
        title="EU Energy Efficiency Directive Recast — Data Centre PUE Targets",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/dir/2024/data-centre",
        reg_id="(EU) 2024/DC",
        snippet="数据中心能效指令",
        full_text=(
            "Recast of the Energy Efficiency Directive imposes PUE (Power Usage Effectiveness) "
            "targets and reporting on data centres above 100 kW IT load. "
            "Applies exclusively to enterprise data center facilities and cloud services. "
            "Excludes consumer electronics, transportation, and outdoor power equipment."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N04_marine_engine",
        title="IMO MEPC Resolution — Marine Diesel Engine NOx Limits",
        market="全球通用",
        source_url="https://www.imo.org/MEPC/marine-engines",
        reg_id="MEPC-XXX",
        snippet="国际海事组织船用柴油机排放",
        full_text=(
            "International Maritime Organization Marine Environment Protection Committee "
            "Resolution MEPC.XXX on Tier III NOx emission limits for marine diesel engines "
            "above 130 kW installed on ships of 24 metres or more in length. "
            "Applies exclusively to maritime vessel propulsion systems."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N05_aviation_drone_far107",
        title="FAA 14 CFR Part 107 Update — Commercial UAS Operations",
        market="美国",
        source_url="https://www.faa.gov/uas/commercial_operators/part_107",
        reg_id="14 CFR Part 107",
        snippet="FAA 商用无人机操作规则",
        full_text=(
            "Federal Aviation Administration update to 14 CFR Part 107 on small "
            "Unmanned Aircraft Systems (UAS) commercial operations. "
            "Applies to drones under 55 lbs operated for commercial purposes. "
            "Excludes ground-based personal mobility devices and consumer electronics."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N06_kitchen_appliance",
        title="EU Ecodesign Regulation — Kitchen Hoods and Domestic Ovens",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg/2024/kitchen",
        reg_id="(EU) 2024/KIT",
        snippet="欧盟厨电生态设计法规",
        full_text=(
            "Commission Regulation establishing ecodesign requirements for kitchen hoods, "
            "ovens, and dishwashers under the Ecodesign Framework Directive. "
            "Applies to white goods kitchen appliances only. "
            "Does not affect mobility devices, garden machinery, or consumer electronics."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N07_bathroom_fittings",
        title="DIN EN 200 — Sanitary Tapware Specifications",
        market="德国",
        source_url="https://www.din.de/en/getting-involved/standards-committees/nas/standards/en-200",
        reg_id="DIN EN 200",
        snippet="德国卫浴龙头标准",
        full_text=(
            "DIN EN 200 specifies general technical requirements for sanitary tapware "
            "in residential bathrooms and kitchens. Pressure ratings, flow rates, durability tests."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N08_industrial_robot",
        title="ISO 10218-1 — Industrial Robot Safety",
        market="全球通用",
        source_url="https://www.iso.org/standard/iso-10218",
        reg_id="ISO 10218-1",
        snippet="工业机器人安全",
        full_text=(
            "ISO 10218-1 specifies safety requirements for industrial robots used in factory "
            "manufacturing settings. Applies to articulated arm robots, SCARA robots, "
            "delta robots in production lines. Not applicable to consumer products, "
            "service robots, lawn care robots, or personal mobility devices."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N09_telecom_5g",
        title="ETSI TS 138 5G New Radio Specifications",
        market="欧盟",
        source_url="https://www.etsi.org/standards/138-5g-nr",
        reg_id="ETSI TS 138",
        snippet="5G 新空口规范",
        full_text=(
            "ETSI Technical Specification TS 138 defines 5G New Radio (5G NR) physical "
            "layer specifications for mobile network base stations and user equipment radio modems. "
            "Applies to telecom infrastructure and smartphones."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N10_pharma_gmp",
        title="EU GMP Annex 15 — Qualification and Validation",
        market="欧盟",
        source_url="https://ec.europa.eu/health/eudralex/vol-4/annex-15",
        reg_id="EU GMP A15",
        snippet="欧盟药品 GMP 验证",
        full_text=(
            "EU Good Manufacturing Practice Annex 15 on qualification and validation "
            "of pharmaceutical manufacturing processes. Applies to medicinal products "
            "and active pharmaceutical ingredients."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N11_construction_steel",
        title="EN 10025 — Hot Rolled Structural Steel",
        market="欧盟",
        source_url="https://standards.cencenelec.eu/en/10025",
        reg_id="EN 10025",
        snippet="欧盟结构钢标准",
        full_text=(
            "EN 10025 specifies technical delivery conditions for hot-rolled products of "
            "non-alloy structural steels for construction. Applies to building and bridge "
            "structural members."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N12_food_safety",
        title="FDA Food Safety Modernization Act — Produce Safety Rule Update",
        market="美国",
        source_url="https://www.fda.gov/food/food-safety-modernization-act-fsma",
        reg_id="FSMA-PSR",
        snippet="FDA 食品安全现代化法修订",
        full_text=(
            "FDA Produce Safety Rule under FSMA update on agricultural water testing requirements "
            "for fresh fruits and vegetables. Applies to farms and food production facilities."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N13_truck_emissions",
        title="EPA Clean Trucks Rule — Class 7-8 Heavy Duty Vehicles",
        market="美国",
        source_url="https://www.epa.gov/regulations-emissions/clean-trucks",
        reg_id="EPA-CTR",
        snippet="EPA 重型卡车排放规则",
        full_text=(
            "EPA Clean Trucks Rule establishes new NOx and PM emission standards for "
            "heavy-duty diesel trucks and buses (Class 7-8, GVWR > 26,000 lbs). "
            "Does not apply to motorcycles, mopeds, or light-duty vehicles."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N14_furniture_flammability",
        title="UK FFR — Furniture and Furnishings Fire Safety Update 2026",
        market="英国",
        source_url="https://www.gov.uk/furniture-fire-safety-2026",
        reg_id="UK FFR",
        snippet="英国家具阻燃法规更新",
        full_text=(
            "Update to The Furniture and Furnishings (Fire Safety) Regulations covering "
            "upholstered sofas, mattresses, and headboards sold in the UK. Match test, "
            "cigarette test requirements."
        ),
        should_appear_in_report=False,
    ),
    SyntheticReg(
        id="N15_satellite_isro",
        title="ISRO Satellite Communication Spectrum Allocation 2026",
        market="印度",
        source_url="https://www.isro.gov.in/spectrum-allocation",
        reg_id="ISRO 2026-SAT",
        snippet="印度卫星频谱分配",
        full_text=(
            "Indian Space Research Organisation announces revised spectrum allocation for "
            "geostationary satellite services in Ku and Ka bands. Applies to satellite "
            "operators and ground station equipment."
        ),
        should_appear_in_report=False,
    ),
]


# ── 10 条边缘 case（特殊组合，测系统鲁棒性）──────────────────────────────────
EDGE_CASES: list[SyntheticReg] = [
    # 同 reg_id 多条入库（Stage 0 应合并）
    SyntheticReg(
        id="E01_eu_battery_dup_a",
        title="EU Battery Regulation 2023/1542 — Carbon Footprint Methodology",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg/2023/1542/oj#carbon",
        reg_id="(EU) 2023/1542",
        snippet="同 P01 不同写法 + 重点章节",
        full_text=(
            "Article 7 of Regulation (EU) 2023/1542 on batteries specifies the methodology "
            "for carbon footprint declaration of LMT (light means of transport) batteries. "
            "Mandatory from 2025-08-18 for batteries placed on the EU market."
        ),
        expected_products=["电助力自行车", "电动摩托车", "电动滑板车", "电动平衡车"],
        expected_dimensions=["PROD", "RD"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),
    SyntheticReg(
        id="E02_eu_battery_dup_b",
        title="Regulation 2023/1542 Article 64 — Battery Passport Implementation",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg/2023/1542/oj#passport",
        reg_id="Reg 2023/1542",  # 同上但写法不同
        snippet="同 P01 的 Battery Passport 章节",
        full_text=(
            "Article 64 of EU Battery Regulation 2023/1542 establishes the Battery Passport "
            "for LMT batteries. Each battery placed on the market must have a digital passport "
            "with QR code linking to material composition, carbon footprint, and recycling info. "
            "Effective: 2027-02-18."
        ),
        expected_products=["电助力自行车", "电动摩托车", "电动滑板车", "电动平衡车"],
        expected_dimensions=["CERT", "EOL"],
        expected_impact="🔴",
        expected_markets="欧盟",
    ),

    # 多产品 + 多市场 +
    SyntheticReg(
        id="E03_global_micromobility",
        title="OECD International Standard on Personal Light Electric Vehicles (PLEV)",
        market="全球通用",
        source_url="https://www.oecd.org/transport/plev-standard-2026",
        reg_id="OECD/PLEV/2026",
        snippet="OECD 个人轻型电动车标准（覆盖整个微出行品类）",
        full_text=(
            "OECD recommends international harmonized testing standard for "
            "Personal Light Electric Vehicles (PLEV) covering e-scooters, hoverboards, "
            "self-balancing scooters, e-bikes, and electric mopeds. "
            "Affects manufacturers selling into OECD member countries (US, EU, UK, Japan, Korea, Canada, Australia)."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🟡",  # 推荐性国际标准
        expected_markets="全球通用",
    ),

    # 极短文本（导航页/占位）→ 应被 requeue_navigation_failures 拦下
    SyntheticReg(
        id="E04_short_navigation_page",
        title="EU Cyber Resilience Act 2024/2847",
        market="欧盟",
        source_url="https://ec.europa.eu/cra-overview",
        reg_id="(EU) 2024/2847",
        snippet="抓到的是首页占位",
        full_text=(
            "Cyber Resilience Act\n"
            "Home | About | News | Contact\n"
            "EU Commission digital strategy"
        ),  # 70 字符，<800 阈值 → requeue_navigation_failures 应转入合成队列
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🟡",  # 合成路径强制不超过 🟡
        expected_markets="欧盟",
    ),

    # 合成内容（[Gemini synthesis] 前缀）
    SyntheticReg(
        id="E05_synthesized_content",
        title="EU Battery Regulation 2023/1542 Article 7 (synthesized)",
        market="欧盟",
        source_url="https://example.com/synth",
        reg_id="(EU) 2023/1542",
        snippet="原文 fetch 失败，走合成",
        full_text=(
            "[Gemini synthesis]\n"
            "EU Regulation 2023/1542 Article 7 establishes mandatory carbon footprint "
            "declaration for batteries placed on the EU market. "
            "Applies to LMT (light means of transport) batteries used in e-bikes and similar."
        ),
        expected_products=["电助力自行车", "电动摩托车", "电动滑板车", "电动平衡车"],
        expected_dimensions=["PROD"],
        expected_impact="🟡",  # 合成路径强制不超过 🟡
        expected_markets="欧盟",
    ),

    # 中文标题 + 英文原文
    SyntheticReg(
        id="E06_zh_title_en_text",
        title="韩国电动平衡车 KC 认证强化通告",
        market="韩国",
        source_url="https://motie.go.kr/notice/balance-board",
        reg_id="KC-BB-2026",
        snippet="韩国电动平衡车 KC 认证强化",
        full_text=(
            "MOTIE Notice on enhanced KC certification for electric balance boards "
            "(self-balancing scooters / hoverboards). New testing baseline aligned with "
            "UL 2272 effective from 2027-Q1. Mandatory third-party lab testing. "
            "Importers must register the product before customs clearance."
        ),
        expected_products=["电动平衡车"],
        expected_dimensions=["CERT", "IMPORT"],
        expected_impact="🔴",
        expected_markets="韩国",
    ),

    # LLM 故意返回 list 类型 affected_products（之前会崩，已修）
    SyntheticReg(
        id="E07_llm_returns_list",
        title="EU AI Act — High-risk AI in autonomous lawn mowers",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/ai-act-mowers",
        reg_id="(EU) 2024/AIA",
        snippet="LLM 故意把 affected_products 返回成 list，测稳健性",
        full_text=(
            "Regulation (EU) 2024/AIA on Artificial Intelligence applies to autonomous "
            "navigation systems in robotic lawn mowers when used in public spaces. "
            "High-risk AI categorization triggers conformity assessment with notified body. "
            "Effective: 2026-08-01."
        ),
        expected_products=["智能割草机"],
        expected_dimensions=["RD", "CERT"],
        expected_impact="🟡",
        expected_markets="欧盟",
        trap_kind="list_products",
    ),

    # SAFETY 屏蔽（应抛 BlockedResponseError,不静默成低置信猜测入库）
    SyntheticReg(
        id="E08_safety_blocked",
        title="China Export Control on Dual-Use Lithium Battery Tech",
        market="中国",
        source_url="https://www.mofcom.gov.cn/export-control-battery",
        reg_id="MOFCOM-EC-2026",
        snippet="测 SAFETY 屏蔽场景",
        full_text=(
            "Ministry of Commerce export control list update on dual-use technologies "
            "including high energy density lithium-ion batteries above 300 Wh/kg. "
            "Affects export of LEV battery packs above this threshold. "
            "Effective: 2026-07-01. License required for export to listed countries."
        ),
        expected_products=["电助力自行车", "电动摩托车"],
        expected_dimensions=["IMPORT"],
        expected_impact="🟡",
        expected_markets="中国",
        trap_kind="safety_block",
    ),

    # 矛盾合并（LLM 给出包含"虽然/分别针对"的合并 reason，应被拒收）
    SyntheticReg(
        id="E09_contradictory_merge_a",
        title="EU CRA 2024/2847 — Software Update Requirements",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/reg/2024/2847/oj#sw",
        reg_id="(EU) 2024/2847",
        snippet="测 Pass 2 矛盾合并拒收",
        full_text=(
            "Article 13 of CRA requires manufacturers to provide security updates for "
            "the support period of the product. Applies to e-bikes with companion apps."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["RD"],
        expected_impact="🟡",
        expected_markets="欧盟",
        trap_kind="contradictory_merge",
    ),
    SyntheticReg(
        id="E10_contradictory_merge_b",
        title="EU PLD 2024/XXXX — Product Liability for Connected Devices",
        market="欧盟",
        source_url="https://eur-lex.europa.eu/eli/dir/2024/PLD/oj",
        reg_id="(EU) 2024/PLD",  # 与 E09 不同 reg_id 但同维度，LLM 可能合并
        snippet="测 Pass 2 矛盾合并拒收（独立法规不应合并）",
        full_text=(
            "Revised Product Liability Directive extends strict liability to defective "
            "software in connected products including e-bikes and robotic mowers. "
            "Distinct legislative instrument — separate Article and obligations from CRA."
        ),
        expected_products=["电助力自行车", "电动滑板车", "电动平衡车", "电动摩托车", "智能割草机"],
        expected_dimensions=["RD", "ENFORCE"],
        expected_impact="🔴",
        expected_markets="欧盟",
        trap_kind="contradictory_merge",
    ),

    # 独立 reg_id 的短文本（真正能触发 fallback 路径——不会被 Stage 0 合并）
    SyntheticReg(
        id="E11_orphan_short_text",
        title="Spain RD-XXXX — Patinetes Eléctricos en Vías Urbanas",
        market="西班牙",
        source_url="https://www.boe.es/dummy/RD-2026-XXXX",
        reg_id="ES RD-2026-XXXX",
        snippet="西班牙城市电动滑板车法规",
        full_text="BOE — Real Decreto sobre patinetes eléctricos\nInicio | Sumario | Contacto",  # 70 chars
        expected_products=["电动滑板车"],
        expected_dimensions=["USE"],
        expected_impact="🟡",
        expected_markets="西班牙",
    ),
]


ALL_CASES: list[SyntheticReg] = POSITIVE_CASES + NEGATIVE_CASES + EDGE_CASES


def by_id(case_id: str) -> SyntheticReg | None:
    for c in ALL_CASES:
        if c.id == case_id:
            return c
    return None


def expected_relevant_count() -> int:
    return sum(1 for c in ALL_CASES if c.should_appear_in_report)


def expected_irrelevant_count() -> int:
    return sum(1 for c in ALL_CASES if not c.should_appear_in_report)


if __name__ == "__main__":
    print(f"Total cases: {len(ALL_CASES)}")
    print(f"  Positive (should appear): {expected_relevant_count()}")
    print(f"  Negative (should NOT appear): {expected_irrelevant_count()}")
    print(f"  Edge cases: {len(EDGE_CASES)}")
