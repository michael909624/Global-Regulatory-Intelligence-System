"""
Researcher：Gemini Flash + Google Search grounding 发现层。

只做发现，不做合规解读。每条入库到 raw_search_results（scrape_status='待抓取'），
后续由 scraper 抓正文，再由 analyzer 做结构化合规分析。

调用结构：4 个全球调用（D1–D4 各一次）。
非英语市场（日 / 韩 / 俄）不再单独跑——在每次维度搜索内显式要求模型用各国
本地语言（日本語 / 한국어 / русский 等）查询当地官方机构与法规框架，
由模型按语言特点自行适配。
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
from datetime import datetime, timedelta

import ai_client
import prompts
from database import init_db, get_connection
from utils import get_logger, parse_json_array, reg_hash

_log = get_logger("researcher")

# ── 搜索时间窗 ────────────────────────────────────────────────────────────────
NEW_PUBLICATION_DAYS   = 90    # Track A：近 90 天新发布
UPCOMING_DEADLINE_DAYS = 365   # Track B：未来 365 天即将生效

# ── 合规维度 ──────────────────────────────────────────────────────────────────
DIMENSIONS: dict[int, dict] = {
    1: {
        "name": "D1 产品准入（B 端义务）",
        "subtopics": [
            "法定分类：以功率/速度/重量/自动化等级如何划分？是否有新分级或边界调整？",
            "主管机构动态：型式认证机构 / 安全监管机构最近发布的官方通知、决议、Q&A、指引",
            "市场准入流程：型式认证 / 产品注册 / 合格评定的程序变更（数字提交、简化路径等）",
            "进口商 / 经销商义务：标签、技术文档、经济运营商注册、召回程序、尽职调查要求",
        ],
    },
    2: {
        "name": "D2 产品技术合规",
        "subtopics": [
            "机械 / 电气安全：EN / IEC / UL / JIS 等针对电动出行机器及电气消费品的新版/修订",
            "网络安全 / 软件安全：EU CRA、UK PSTI、US NIST IoT 标签、日本 IoT 安全准则",
            "无线 / EMC：EU RED 委托法规、FCC Part 15、日本 MIC、韩国 KCC 型式认证更新",
            "AI 与自动驾驶（户外自主设备 / 联网 LEV）：EU AI Act 实施细则、ISO 25119、IEC 61508",
            "化学品 / 有害物质：RoHS Annex II、REACH SVHC 候选清单、Prop 65、J-MOSS、K-RoHS",
            "标签 / 数字产品护照 / 可持续披露：能源标签、DPP、可维修评分、CSRD 供应链尽职调查",
        ],
    },
    3: {
        "name": "D3 锂电池专项（全生命周期）",
        "subtopics": [
            "电池安全标准：IEC 62133、UL 2271/2272/2849、UN 38.3、GB/T 36972/38031、EN 50604",
            "电池市场准入：EU Battery Regulation 2023/1542 实施细则（碳足迹、尽调、电池护照、回收材料阈值）；UK / AU EESS / 美各州 / 加拿大类似规则",
            "电池运输：IATA DGR 新版、IMDG Code、ADR/RID、US DOT/PHMSA",
            "充电安全 / 接口：充电器强制标准、USB-C 通用充电器扩展、智能充电 / V2G 互操作规则",
            "电池 EPR / 回收：生产者责任登记目标、回收方案规则（EU WEEE/Battery、UK、加拿大省级、澳州）",
            "电池消防：公寓 / 停车场 / 商业充电消防规范，室内储存禁令，喷淋 / 抑制要求",
        ],
    },
    4: {
        "name": "D4 消费者路权与渠道合规（C 端及零售端）",
        "subtopics": [
            "消费者上路前置：强制第三方责任险、车辆登记/牌照、头盔、年龄、限速等终端使用要求",
            "零售 / 电商合规：购买前年龄/资格验证、销售限制、强制安全/健康警示展示",
            "执法行动：政府执法行动、罚款通知、产品召回、市场监管行动、违规车辆查扣令",
        ],
    },
}

# ── 产品覆盖范围 ──────────────────────────────────────────────────────────────
CATEGORY_GROUPS: dict[str, list[str]] = {
    "整机": [
        "电动滑板车（含共享/租赁场景） | electric scooter / e-scooter / kick scooter / shared / dockless"
        " | trottinette électrique | Elektroroller | monopattino elettrico | 電動キックボード | 전동 킥보드 | электросамокат",

        "电动平衡车 | self-balancing scooter / hoverboard / balance board"
        " | gyropode | Hoverboard | セグウェイ | 전동 호버보드",

        "电助力自行车 | electric bicycle / e-bike / pedelec / EPAC / S-Pedelec / PAB"
        " | VAE | E-Fahrrad / Pedelec | bicicletta elettrica | 電動アシスト自転車 | 전기자전거 | электровелосипед"
        " | EU Reg 168/2013 L1e-A · EN 15194",

        "电动摩托车 | electric motorcycle / moped / L1e-B / L3e / electric PTW"
        " | motocyclette électrique | Elektromotorrad | moto elettrica | 電動バイク | 전기 오토바이 | электромотоцикл"
        " | EU Reg 168/2013 L1e-B · L3e",

        "智能割草机 | robotic lawn mower / robot mower / autonomous lawn mower"
        " | tondeuse robot | Mähroboter | robot tagliaerba | ロボット草刈機 | 로봇 잔디깎기",
    ],
    "零部件": [
        "锂电池组（lithium-ion battery pack / module / cell / LEV battery）",
        "电机驱动系统（hub motor / mid-drive / BLDC for e-bike / e-scooter）",
        "控制器（motor controller / ESC / drive controller）",
        "充电器（battery charger / AC-DC / on-board charger / charging station）",
        "BMS 电池管理系统（battery management system / protection circuit）",
    ],
}

# ── 调用参数 ──────────────────────────────────────────────────────────────────

CALL_TIMEOUT = 90      # 单任务超时（秒）
_db_lock     = threading.Lock()


# ── 全球（按维度）──────────────────────────────────────────────────────────────

def _category_block() -> str:
    lines = [f"  • {p}" for p in CATEGORY_GROUPS["整机"]]
    lines.append("  ── 关键零部件 ──")
    lines.extend(f"  • {p}" for p in CATEGORY_GROUPS["零部件"])
    return "\n".join(lines)


def _subtopics_block(subtopics: list[str]) -> str:
    return "\n".join(f"  {i+1}. {s}" for i, s in enumerate(subtopics))


def _build_global_prompt(dim_id: int, quick: bool) -> str:
    today     = datetime.now().strftime("%Y-%m-%d")
    start_new = (datetime.now() - timedelta(days=NEW_PUBLICATION_DAYS)).strftime("%Y-%m-%d")
    end_up    = (datetime.now() + timedelta(days=UPCOMING_DEADLINE_DAYS)).strftime("%Y-%m-%d")
    dim       = DIMENSIONS[dim_id]

    if quick:
        scope = prompts.load("researcher_scope_quick").format(
            new_days=NEW_PUBLICATION_DAYS,
            start_new=start_new,
        )
    else:
        scope = prompts.load("researcher_scope_full").format(
            new_days=NEW_PUBLICATION_DAYS,
            start_new=start_new,
            upcoming_days=UPCOMING_DEADLINE_DAYS,
            end_up=end_up,
        )

    return prompts.load("researcher_global").format(
        today=today,
        dim_name=dim["name"],
        subtopics_block=_subtopics_block(dim["subtopics"]),
        category_block=_category_block(),
        scope=scope,
        output_schema=prompts.load("researcher_output_schema"),
    )


# ── Gemini grounding 调用 ─────────────────────────────────────────────────────
# 发现层:高 temperature 最大化召回率(混沌搜索)。
# 一致性靠下游去重 + 累积数据库,不靠单次调用稳定。

def _call(prompt: str) -> tuple[str, list[dict]]:
    return ai_client.call_grounded(
        prompt,
        system=prompts.load("researcher_system"),
        # temperature / top_p 用 ai_client 的默认值(1.0 / 0.95)
    )


# ── 入库 ───────────────────────────────────────────────────────────────────────

def _store(reg: dict, sources: list[dict]) -> bool:
    """保存一条发现到 raw_search_results；按 reg_hash(title) 去重。"""
    title = (reg.get("title_original") or reg.get("title") or "").strip()
    if not title:
        return False

    h      = reg_hash(title)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()

    with _db_lock:
        with get_connection() as conn:
            # 跨表去重：30 天窗口内任一表已有同 hash → 跳过
            if conn.execute(
                "SELECT 1 FROM raw_search_results WHERE content_hash=? AND query_date>=?",
                (h, cutoff),
            ).fetchone():
                return False
            if conn.execute(
                "SELECT 1 FROM compliance_analysis WHERE content_hash=? AND analysis_date>=?",
                (h, cutoff),
            ).fetchone():
                return False

            title_cn      = (reg.get("title_cn") or "").strip()[:25]   # 兜底截断（prompt 要求 ≤20，留余量）
            explicit_url  = (reg.get("url") or "").strip()
            grounding_urls = [
                s["url"] for s in sources
                if s.get("url") and "vertexaisearch" not in s["url"]
            ]
            # 主 URL：优先用模型给的 explicit URL（语义最准确），否则用第一个 grounding。
            # 主 URL 之外的 grounding URL 作为 fallback，scraper 主 URL 失败时按序回退。
            seen      = {explicit_url} if explicit_url else set()
            fallbacks: list[str] = []
            for u in grounding_urls:
                if u and u not in seen:
                    seen.add(u)
                    fallbacks.append(u)
            if explicit_url:
                rep_url = explicit_url
            elif fallbacks:
                rep_url   = fallbacks[0]
                fallbacks = fallbacks[1:]
            else:
                rep_url = ""
            fallback_json = json.dumps(fallbacks[:5], ensure_ascii=False) if fallbacks else None

            market_hint  = (reg.get("market_hint") or "").strip()
            relevance    = (reg.get("relevance_note") or "").strip()

            conn.execute("""
                INSERT OR IGNORE INTO raw_search_results
                    (query_date, source_url, title, title_cn, snippet, priority,
                     product_category, market, content_hash, scrape_status,
                     fallback_urls)
                VALUES (?,?,?,?,?,'高',null,?,?,'待抓取',?)
            """, (
                datetime.now().isoformat(),
                rep_url, title, title_cn, relevance[:500], market_hint,
                h, fallback_json,
            ))
            return bool(conn.execute("SELECT changes()").fetchone()[0])


# ── 主入口 ────────────────────────────────────────────────────────────────────

def run_research(quick: bool = False) -> tuple[int, int]:
    """
    并发执行 4 个 Gemini 调用（D1–D4 各一次），入库到 raw_search_results。
    每个调用内由模型自行用各法域本地语言（含日韩俄）查询。
    开始前先注入种子库(seeds.py)做保底召回。
    返回 (inserted, skipped) — 含种子注入数量。
    """
    init_db()

    # 种子保底:不依赖 AI,先把已知权威源塞进队列。
    from seeds import inject_seeds, SEEDS
    seed_in, seed_skip = inject_seeds()
    print(f"\n  种子库:{len(SEEDS)} 条 → 新增 {seed_in},已存在跳过 {seed_skip}")

    tasks: list[tuple[str, str]] = [
        (_build_global_prompt(dim_id, quick), f"全球 · {dim['name']}")
        for dim_id, dim in DIMENSIONS.items()
    ]

    total = len(tasks)
    inserted = seed_in
    skipped  = seed_skip
    failed   = 0
    mode = "快速（仅新发布）" if quick else "全量（新发布 + 即将生效）"

    print(f"\n  模式：{mode}")
    print(f"  共 {total} 个任务（D1–D4 维度，AI 自行按法域适配本地语言）\n")

    def _process_task(prompt: str) -> tuple[int, int, int]:
        text, srcs = _call(prompt)
        regs       = parse_json_array(text) or []
        n_in = n_sk = 0
        for reg in regs:
            if isinstance(reg, dict) and _store(reg, srcs):
                n_in += 1
            else:
                n_sk += 1
        return n_in, n_sk, len(regs)

    completed = 0
    future_to_label: dict = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=5, thread_name_prefix="researcher"
    ) as executor:
        for prompt, label in tasks:
            fut = executor.submit(_process_task, prompt)
            future_to_label[fut] = label

        for fut in concurrent.futures.as_completed(future_to_label):
            completed += 1
            label = future_to_label[fut]
            print(f"  [{completed:>2}/{total}] {label}", end=" ... ", flush=True)
            try:
                n_in, n_sk, n_regs = fut.result(timeout=CALL_TIMEOUT)
                inserted += n_in
                skipped  += n_sk
                _log.info("OK %s regs=%d new=%d dup=%d", label, n_regs, n_in, n_sk)
                print(f"发现 {n_regs} 条  →  入库 {n_in}，重复 {n_sk}")
            except (TimeoutError, concurrent.futures.TimeoutError):
                failed += 1
                _log.warning("TIMEOUT %s", label)
                print(f"⏱ 跳过：超时（>{CALL_TIMEOUT}s）")
            except Exception as e:
                failed += 1
                _log.error("FAIL %s  %s", label, e)
                print(f"❌  {e}")

    suffix = f"  （{failed} 个任务调用失败）" if failed else ""
    print(f"\n  总计：入库 {inserted} 条待抓取，重复跳过 {skipped} 条{suffix}")
    return inserted, skipped


if __name__ == "__main__":
    import sys
    run_research(quick="--quick" in sys.argv)
