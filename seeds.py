"""
Seeds — 高权威源种子库。

作用:不依赖 AI 发现,直接把已知权威源的"待审查链接"塞进抓取队列。
对应公司业务:轻型电动出行(LEV)整机 + 锂电池 + 电机 + 充电器 + BMS。

设计原则:
  • 每条种子是一个 *入口页* 或 *搜索结果页*,scraper 抓回来后,
    analyzer 会基于其文本判断是否相关、做合规分析。
  • 种子的目的是"保底召回",防止 AI 偶尔漏掉头部权威源。
  • AI 发现层(researcher)与种子互补,不冲突 — 都入同一队列,
    后续按 reg_hash(title) 去重。

如何维护:
  • 每月抽 5 分钟,审查 SEEDS 里的链接是否仍可用。
  • 发现新的权威源,加到对应分组。

如何使用:
  • python gris.py seed             # 只注入种子(不调用 AI)
  • python gris.py run              # 自动包含种子注入 + AI 发现
"""
from __future__ import annotations

import json
from datetime import datetime

from database import get_connection, init_db
from utils import get_logger, reg_hash

_log = get_logger("seeds")


# ── 种子定义 ──────────────────────────────────────────────────────────────────
#
# 字段说明:
#   title    — 种子条目标题(用于去重 hash 与 Excel 显示)
#   url      — 入口页 URL(scraper 会抓这个页面)
#   market   — 主要适用市场
#   note     — 一句话说明此源的价值(给 analyzer 当 relevance_note)
#
# 注:title 必须稳定,改 title 会导致重新入库;改 url 不影响 hash。

SEEDS: list[dict] = [
    # ── 美国 ───────────────────────────────────────────────────────────────
    {
        "title": "US Federal Register — e-bike / e-scooter / battery rules",
        "url":   "https://www.federalregister.gov/documents/search?conditions[term]=e-bike+OR+e-scooter+OR+lithium+battery&conditions[type][]=RULE&conditions[type][]=PRORULE",
        "market": "美国",
        "note":  "美国联邦公报 — 电动两轮车与锂电池相关的最终规则与拟议规则。",
    },
    {
        "title": "US CPSC — Recalls & Compliance (lithium / e-mobility)",
        "url":   "https://www.cpsc.gov/Recalls",
        "market": "美国",
        "note":  "美国消费品安全委员会 — 锂电池与电动出行产品召回与合规通告。",
    },
    {
        "title": "NHTSA — motor vehicle defects & compliance",
        "url":   "https://www.nhtsa.gov/recalls",
        "market": "美国",
        "note":  "美国 NHTSA — 机动车(含电摩)缺陷与合规决议。",
    },

    # ── 欧盟 ───────────────────────────────────────────────────────────────
    {
        "title": "EUR-Lex — recent acts on batteries / type-approval",
        "url":   "https://eur-lex.europa.eu/search.html?qid=&text=battery+OR+type+approval&scope=EURLEX&type=quick&lang=en",
        "market": "欧盟",
        "note":  "EUR-Lex — 欧盟法规检索(电池法规、Reg 168/2013 型式认证等更新)。",
    },
    {
        "title": "EU Battery Regulation — implementing & delegated acts",
        "url":   "https://environment.ec.europa.eu/topics/waste-and-recycling/batteries-and-accumulators_en",
        "market": "欧盟",
        "note":  "欧盟电池法规 (2023/1542) 实施细则与委托法规更新页。",
    },
    {
        "title": "UNECE WP.29 — vehicle regulations (incl. L-category)",
        "url":   "https://unece.org/transport/vehicle-regulations",
        "market": "全球",
        "note":  "UNECE WP.29 — 全球机动车技术法规(含 L1e/L3e 电摩/电助力)。",
    },
    {
        "title": "EU Commission — Cyber Resilience Act resources",
        "url":   "https://digital-strategy.ec.europa.eu/en/policies/cyber-resilience-act",
        "market": "欧盟",
        "note":  "EU CRA 网络安全法 — 涉及联网 LEV 与 BMS 的实施动态。",
    },

    # ── 英国 ───────────────────────────────────────────────────────────────
    {
        "title": "UK OPSS — product safety news & alerts",
        "url":   "https://www.gov.uk/government/organisations/office-for-product-safety-and-standards",
        "market": "英国",
        "note":  "英国 OPSS — 消费品安全(锂电池火灾、电动滑板车等)动态。",
    },

    # ── 中国 ───────────────────────────────────────────────────────────────
    {
        "title": "SAMR / CCC — recent technical regulations",
        "url":   "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/zcfg/",
        "market": "中国",
        "note":  "国家市场监管总局 — 强制性认证(CCC)、电动自行车标准更新。",
    },
    {
        "title": "GB Standards — e-bike & lithium battery (national)",
        "url":   "https://openstd.samr.gov.cn/bzgk/gb/",
        "market": "中国",
        "note":  "国家标准全文公开 — GB 17761(电动自行车)、GB/T 36972 等。",
    },

    # ── 日本 ───────────────────────────────────────────────────────────────
    {
        "title": "METI — 経産省 PSE / 電気用品安全法",
        "url":   "https://www.meti.go.jp/policy/consumer/seian/denan/index.html",
        "market": "日本",
        "note":  "日本经济产业省 — PSE 认证、电气用品安全法相关公告。",
    },
    {
        "title": "MLIT — 国交省 道路運送車両法 / 特定小型原付",
        "url":   "https://www.mlit.go.jp/jidosha/jidosha_fr1_000043.html",
        "market": "日本",
        "note":  "日本国土交通省 — 特定小型原付(电动滑板车)与道路运送车辆法。",
    },

    # ── 韩国 ───────────────────────────────────────────────────────────────
    {
        "title": "KATS — 국가기술표준원 KC certification",
        "url":   "https://www.kats.go.kr/content.do?cmsid=11",
        "market": "韩国",
        "note":  "韩国国家技术标准院 — KC 认证、전기용품 안전관리법 公告。",
    },

    # ── 加拿大 / 澳新 ───────────────────────────────────────────────────────
    {
        "title": "Health Canada / Transport Canada — recent recalls",
        "url":   "https://recalls-rappels.canada.ca/en",
        "market": "加拿大",
        "note":  "加拿大 — 消费品与机动车召回(含电池、电动滑板车)。",
    },
    {
        "title": "ACCC — Australian product safety",
        "url":   "https://www.productsafety.gov.au/news",
        "market": "澳大利亚",
        "note":  "澳大利亚 ACCC — 产品安全(锂电池新规)与禁令。",
    },
]


# ── 注入 ──────────────────────────────────────────────────────────────────────

def inject_seeds() -> tuple[int, int]:
    """
    把所有 SEEDS 入库到 raw_search_results,scrape_status='待抓取'。

    去重:基于 reg_hash(title);已存在则跳过。
    返回 (inserted, skipped)。
    """
    init_db()
    inserted = skipped = 0

    with get_connection() as conn:
        for s in SEEDS:
            title  = s["title"].strip()
            url    = s["url"].strip()
            market = s.get("market", "").strip()
            note   = s.get("note", "").strip()
            h      = reg_hash(title)

            # 如果近期已入库过同 hash,跳过
            if conn.execute(
                "SELECT 1 FROM raw_search_results WHERE content_hash=?",
                (h,),
            ).fetchone():
                skipped += 1
                continue
            if conn.execute(
                "SELECT 1 FROM compliance_analysis WHERE content_hash=?",
                (h,),
            ).fetchone():
                skipped += 1
                continue

            conn.execute("""
                INSERT OR IGNORE INTO raw_search_results
                    (query_date, source_url, title, title_cn, snippet, priority,
                     product_category, market, content_hash, scrape_status,
                     fallback_urls)
                VALUES (?,?,?,?,?,'高',null,?,?,'待抓取',?)
            """, (
                datetime.now().isoformat(),
                url, title, title[:25], note[:500], market,
                h, None,
            ))
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
            else:
                skipped += 1

    _log.info("Seeds injected: new=%d skipped=%d total=%d",
              inserted, skipped, len(SEEDS))
    return inserted, skipped


def run_seed_command() -> None:
    """CLI 入口:python gris.py seed"""
    print(f"\n  种子库共 {len(SEEDS)} 条权威源,正在注入待抓取队列...")
    new, skip = inject_seeds()
    print(f"\n  完成:新增 {new} 条,已存在跳过 {skip} 条。")
    print(f"  下一步:运行 python gris.py scrape 抓取这些种子。\n")


if __name__ == "__main__":
    run_seed_command()
