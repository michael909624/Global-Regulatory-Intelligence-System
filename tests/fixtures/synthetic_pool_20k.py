"""
20000 条规模信息池：在 5K 池子四类基础上扩展 3 类失败模式。

新增标签（标准答案）：
  • BAD_URL_RELEVANT     URL 失效但内容应进周报：sc 不写入 + scrape_status='失败'
                         考验 fallback grounded 合成路径能否救回
  • BAD_URL_404          URL 失效且无救回价值（占位编号 / 模糊 title）：
                         考验 fallback._should_fallback 启发式过滤能丢弃
  • TITLE_URL_MISMATCH   title 像合规法规但 sc 是无关短段落（典型导航页/错页）：
                         考验 requeue_navigation_failures 检测短文本能拦下

20K 分布：
    SHOULD_APPEAR        100   时间窗内 + 业务相关 + URL 完好（应进周报）
    BAD_URL_RELEVANT      50   时间窗内 + 业务相关 + URL 失效（fallback 救回）
    OUT_OF_WINDOW       1500   业务相关但出时间窗
    DISTRACTOR          2500   标题像但内容明确不沾
    TITLE_URL_MISMATCH   250   title 含合规关键词但内容是无关段落
    BAD_URL_404          100   URL 失效 + 占位编号 / 模糊 title（应被启发式过滤）
    IRRELEVANT         15500   完全不相关
    总计               20000

应抓回（分母）= SHOULD_APPEAR + BAD_URL_RELEVANT = 150
"""
from __future__ import annotations

import random
from datetime import datetime

from .synthetic_pool_5k import (
    PoolReg,
    _MARKETS_DICT, _PRODUCTS_FULL, _RELEVANT_TEMPLATES,
    _DISTRACTOR_TEMPLATES, _IRRELEVANT_TEMPLATES, _IRRELEVANT_TOPICS,
    _hash_id, _date_in_window, _date_out_window, _pick_products,
    _dedupe_titles,
    _gen_in_window_relevant, _gen_out_window_relevant,
    _gen_distractors, _gen_irrelevant,
)


_PROD_EN_MAP = {
    "电助力自行车": "E-Bikes",
    "电动滑板车": "E-Scooters",
    "电动平衡车": "Hoverboards",
    "电动摩托车": "Electric Motorcycles",
    "智能割草机": "Robotic Lawn Mowers",
}


# ── BAD_URL_RELEVANT：URL 失效但应进周报 ──────────────────────────────────────
# 模板：title 与 SHOULD_APPEAR 同分布（合规法规 title），但 source_url 模拟死链；
# 测试链路里 sc 不写入 + scrape_status='失败' → fallback grounded 救回。

def _gen_bad_url_relevant(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    out = []
    for i in range(n):
        title_tpl, dims, prod_kind, impact = rng.choice(_RELEVANT_TEMPLATES)
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        prod_zh = rng.choice(_PRODUCTS_FULL)
        prod_en = _PROD_EN_MAP[prod_zh]
        std_id = f"{rng.choice(['EN', 'UL', 'IEC', 'GB', 'UN R'])}-{rng.randint(50, 99999)}"
        year = today.year + rng.choice([-1, 0, 1])
        title = title_tpl.format(market_en=market_en, prod_en=prod_en, std_id=std_id, year=year)
        publish_date = _date_in_window(rng, today)

        # 死链：路径含 deadlink-XXX，scraper 走不通
        out.append(PoolReg(
            id=f"BR_{i:03d}_{_hash_id((market_zh, prod_zh, std_id, 'badurl', i))}",
            title=title,
            market=market_zh,
            source_url=f"https://broken.example.gov/{market_en.lower().replace(' ', '-')}/deadlink-{std_id}/{i}",
            reg_id=std_id,
            snippet=f"{market_zh} {prod_zh} 法规（URL 已失效，需 grounded 合成）",
            full_text="",  # 不写入 sc——_load 阶段按 scrape_outcome 分流
            publish_date=publish_date,
            label="SHOULD_APPEAR",       # 标"应抓回"——纳入分母
            expected_products=_pick_products(rng, prod_kind),
            expected_dimensions=list(dims),
            expected_impact=impact,
            expected_markets=market_zh,
            scrape_outcome="fail",
        ))
    return out


# ── BAD_URL_404：URL 失效 + 占位编号——预期被启发式过滤掉 ─────────────────────
# fallback._should_fallback 看到 reg_id 含 XXXX/TBD/placeholder 就跳过 → 不进周报。

def _gen_bad_url_404(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    out = []
    placeholders = ["XXXX", "TBD", "TBA", "placeholder"]
    # title 含一个监管关键词但 reg_id 是占位 → 启发式过滤路径
    title_templates = [
        "{market_en} Draft Notice {placeholder}: Untitled Discussion Paper",
        "{market_en} Internal Working Document {placeholder} (Not Yet Numbered)",
        "{market_en} Pre-Consultation Sketch — Reference {placeholder}",
        "{market_en} Drafted Recall Outline — Document {placeholder}",
    ]
    for i in range(n):
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        ph = rng.choice(placeholders)
        title_tpl = rng.choice(title_templates)
        title = title_tpl.format(market_en=market_en, placeholder=ph)
        publish_date = _date_in_window(rng, today)
        out.append(PoolReg(
            id=f"B4_{i:03d}_{_hash_id((market_zh, ph, i))}",
            title=title,
            market=market_zh,
            # reg_id 故意空——_should_fallback 档 A 不通过；title 虽含 notice/recall 关键词
            # 但 _PLACEHOLDER_PATTERNS_RE 命中 XXXX/TBD → 直接被档 C 过滤
            source_url=f"https://broken.example.gov/draft/404/{i}",
            reg_id=None,
            snippet=f"{market_zh} 占位草稿 (URL 失效，无法补救)",
            full_text="",
            publish_date=publish_date,
            label="IRRELEVANT",  # 期望不进周报
            expected_products=[],
            expected_dimensions=[],
            expected_impact="🟡",   # IRRELEVANT 应被"不相关"过滤,impact 取值不重要
            expected_markets="",
            scrape_outcome="fail",
        ))
    return out


# ── TITLE_URL_MISMATCH：title 像合规但 sc 是无关短段落 ──────────────────────────
# 真实场景：scraper 抓回的是网站首页 / 隐私政策 / Cookie 提示等导航页。
# 内容长度 < 800 字符 → requeue_navigation_failures 转入 fallback 队列。
# fallback grounded mock 也返回"找不到内容"——最终 LLM 应判不相关。

_NAV_PAGE_TEXTS = [
    "Page Not Found. The resource you requested has moved or no longer exists. "
    "Return to home page. Privacy Policy. Cookie Settings. Contact Us.",
    "About Us. Our agency was established to ensure consumer protection. "
    "Mission. Vision. Leadership team. Annual reports. Press releases.",
    "Cookie Notice. We use cookies to improve your experience. "
    "Accept all. Reject all. Manage preferences. Read our privacy policy.",
    "Site Map. Home. About. News. Publications. Forms. Contact. Login. Logout. "
    "Frequently Asked Questions. Disclaimers. Legal notices.",
    "Welcome to the Portal. Choose your service area: business, individual, "
    "academic, government. Use the search bar to find specific topics.",
    "Search Results — No matches found. Try different keywords or browse categories. "
    "Suggested links: news archive, recent updates, agency contacts.",
]


def _gen_title_url_mismatch(n: int, rng: random.Random, today: datetime) -> list[PoolReg]:
    out = []
    for i in range(n):
        # 用合规模板生成"看似相关"的 title——LLM 看到 title 易上钩
        title_tpl, _, _, _ = rng.choice(_RELEVANT_TEMPLATES)
        market_zh, market_en = rng.choice(_MARKETS_DICT)
        prod_zh = rng.choice(_PRODUCTS_FULL)
        prod_en = _PROD_EN_MAP[prod_zh]
        std_id = f"{rng.choice(['EN', 'UL', 'IEC', 'GB', 'UN R'])}-{rng.randint(50, 99999)}"
        year = today.year + rng.choice([-1, 0, 1])
        title = title_tpl.format(market_en=market_en, prod_en=prod_en, std_id=std_id, year=year)
        publish_date = _date_in_window(rng, today)

        nav_text = rng.choice(_NAV_PAGE_TEXTS)
        out.append(PoolReg(
            id=f"TM_{i:03d}_{_hash_id((market_zh, prod_zh, std_id, 'mismatch', i))}",
            title=title,
            market=market_zh,
            source_url=f"https://example.gov/{market_en.lower().replace(' ', '-')}/wrong-page/{i}",
            reg_id=std_id,
            snippet=title_tpl[:30],
            full_text=nav_text,  # 短文本 → requeue → fallback
            publish_date=publish_date,
            label="IRRELEVANT",  # 期望不进周报
            expected_products=[],
            expected_dimensions=[],
            expected_impact="🟡",   # IRRELEVANT 应被"不相关"过滤,impact 取值不重要
            expected_markets="",
            scrape_outcome="mismatch",
        ))
    return out


# ── 总入口 ─────────────────────────────────────────────────────────────────────


def generate_pool_20k(
    n_should: int = 100,
    n_bad_url_relevant: int = 50,
    n_out_window: int = 1500,
    n_distractor: int = 2500,
    n_title_url_mismatch: int = 250,
    n_bad_url_404: int = 100,
    n_irrelevant: int = 15500,
    seed: int = 42,
    today: datetime | None = None,
) -> list[PoolReg]:
    rng = random.Random(seed)
    today = today or datetime.now()

    pool = (
        _gen_in_window_relevant(n_should, rng, today)
        + _gen_bad_url_relevant(n_bad_url_relevant, rng, today)
        + _gen_out_window_relevant(n_out_window, rng, today)
        + _gen_distractors(n_distractor, rng, today)
        + _gen_title_url_mismatch(n_title_url_mismatch, rng, today)
        + _gen_bad_url_404(n_bad_url_404, rng, today)
        + _gen_irrelevant(n_irrelevant, rng, today)
    )
    pool = _dedupe_titles(pool)
    rng.shuffle(pool)
    return pool


if __name__ == "__main__":
    pool = generate_pool_20k()
    from collections import Counter
    label_counts = Counter(p.label for p in pool)
    outcome_counts = Counter(p.scrape_outcome for p in pool)
    print(f"Total: {len(pool)}")
    print("\nLabel 分布：")
    for k, v in label_counts.items():
        print(f"  {k:<22}: {v}")
    print("\nscrape_outcome 分布：")
    for k, v in outcome_counts.items():
        print(f"  {k:<22}: {v}")
