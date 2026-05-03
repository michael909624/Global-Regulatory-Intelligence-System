"""
Seeds — 高权威源种子库。

作用:登记一批已知的权威监管入口/索引页,作为系统认知边界的标记。
对应公司业务:轻型电动出行(LEV)整机 + 锂电池 + 电机 + 充电器 + BMS。

设计原则:
  • 种子是 *入口页 / 搜索结果页*,本身不是某条具体法规
    (例:federalregister 搜索 URL、CPSC Recalls 列表、SAMR 法规库索引)
  • 种子 *不参与* scrape → analyze 流水线 — 入库时 priority='种子'、
    scrape_status='已抓取',scraper 与 analyzer 都会跳过它们,不会出现在
    Excel 周报里(否则索引页会被分析成"信息不足 → 不相关"污染报告)。
  • 种子的真正用途:
      1. 占位去重 — researcher 之后若发现同 title hash 直接跳过,避免
         AI 又把这些入口页当作"新发现"重复入库

如何维护:
  • 每月抽 5 分钟,审查 SEEDS 里的链接是否仍可用。
  • 发现新的权威源,加到对应分组。

如何使用:
  • python gris.py seed   # 注入/刷新种子(幂等,可反复调用)
  • python gris.py run    # 自动包含种子注入 + AI 发现
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

def _purge_legacy_seed_artifacts(conn, raw_id: int) -> int:
    """清理旧版本遗留:同一种子 raw_id 关联的 scraped_content + compliance_analysis。

    早期种子会进入 scrape→analyze 管线,产生"信息不足→不相关"的无意义分析。
    新版本种子不再走分析,需要把历史污染数据回收掉。
    返回删除的 scraped_content 行数。
    """
    sc_rows = conn.execute(
        "SELECT id FROM scraped_content WHERE raw_id = ?", (raw_id,)
    ).fetchall()
    if not sc_rows:
        return 0
    sc_ids = [r["id"] for r in sc_rows]
    ph = ",".join("?" * len(sc_ids))
    conn.execute(f"DELETE FROM compliance_analysis WHERE scraped_id IN ({ph})", sc_ids)
    conn.execute(f"DELETE FROM scraped_content     WHERE id         IN ({ph})", sc_ids)
    return len(sc_ids)


def inject_seeds() -> tuple[int, int]:
    """
    把所有 SEEDS 登记到 raw_search_results,priority='种子'、scrape_status='已抓取'。
    种子不参与 scrape/analyze 流水线 — 仅作为占位去重的标记。

    幂等:重复调用会刷新已存在种子的 priority/status,并清掉早期版本遗留的
    scraped_content / compliance_analysis 污染数据。

    返回 (inserted, refreshed)。
    """
    init_db()
    inserted = refreshed = purged_artifacts = 0

    with get_connection() as conn:
        for s in SEEDS:
            title  = s["title"].strip()
            url    = s["url"].strip()
            market = s.get("market", "").strip()
            note   = s.get("note", "").strip()
            h      = reg_hash(title)

            existing = conn.execute(
                "SELECT id, priority, scrape_status FROM raw_search_results "
                "WHERE content_hash=?",
                (h,),
            ).fetchone()

            if existing:
                purged_artifacts += _purge_legacy_seed_artifacts(conn, existing["id"])
                if existing["priority"] != "种子" or existing["scrape_status"] != "已抓取":
                    conn.execute(
                        "UPDATE raw_search_results "
                        "SET priority='种子', scrape_status='已抓取' "
                        "WHERE id=?",
                        (existing["id"],),
                    )
                    refreshed += 1
                continue

            conn.execute("""
                INSERT INTO raw_search_results
                    (query_date, source_url, title, title_cn, snippet, priority,
                     product_category, market, content_hash, scrape_status,
                     fallback_urls)
                VALUES (?,?,?,?,?,'种子',null,?,?,'已抓取',?)
            """, (
                datetime.now().isoformat(),
                url, title, title[:25], note[:500], market,
                h, None,
            ))
            inserted += 1

    _log.info(
        "Seeds injected: new=%d refreshed=%d purged_artifacts=%d total=%d",
        inserted, refreshed, purged_artifacts, len(SEEDS),
    )
    return inserted, refreshed


def run_seed_command() -> None:
    """CLI 入口:python gris.py seed"""
    print(f"\n  种子库共 {len(SEEDS)} 条权威源,正在登记...")
    new, refreshed = inject_seeds()
    print(f"\n  完成:新增 {new} 条,刷新 {refreshed} 条历史种子。")
    print(f"  种子不参与抓取/分析 — 仅作为占位去重的标记。\n")


if __name__ == "__main__":
    run_seed_command()
