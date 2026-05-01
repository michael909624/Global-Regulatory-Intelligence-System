"""
Researcher：议题驱动的三阶段召回。

设计范式：从"自由发现 N 条"转向"系统性扫描已枚举的搜索空间"。

三阶段：
  1. Plan   — 让模型枚举每个维度下的监管议题图（监管面清单，不是具体法规）
  2. Fetch  — 对每个议题独立发起搜索，双温度（t=0.2 + t=1.0）合并去重
              低温捞确凿头部（CRA 类必出），高温捞长尾（小众法域）
  3. Audit  — 对零命中议题用更宽时间窗 + 高温重试，闭环修复漏检

议题图本身不持久化——每次 run 重新枚举（占总耗时 <5%，刷新成本低）。
召回的去重契约（reg_hash + 30 天窗口）保持不变，承接现有 scraper / analyzer。
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse

import ai_client
import prompts
from database import init_db, get_connection
from utils import get_logger, parse_json_array, reg_hash

_log = get_logger("researcher")

# 公共五维业务影响坐标——注入到 plan / fetch system prompt，
# 与 analyzer / consolidation 共享同一套判定语言（端到端闭环）。
_BUSINESS_SCOPE = prompts.load("business_scope")

# ── 搜索时间窗 ────────────────────────────────────────────────────────────────
NEW_PUBLICATION_DAYS   = 90    # Track A：近 90 天新发布
UPCOMING_DEADLINE_DAYS = 365   # Track B：未来 365 天即将生效

# Audit 模式时间窗倍数（零命中议题用更宽窗口扩搜）
_AUDIT_WINDOW_MULTIPLIER = 2

# ── 八维议题图（与 business_scope L3 完全对齐）──────────────────────────────
# Plan 按这八个 L3 维度分别枚举议题。
# L2（售前/售中/售后）由 reporter 端从 L3 派生，无需模型处理。
DIMENSIONS: dict[str, dict] = {
    # ── 售前 ───────────────────────────────────────────────────────────
    "RD": {
        "name": "RD 产品研发设计",
        "subtopics": [
            "机械 / 电气 / 防火 / 阻燃 标准（EN / IEC / UL / JIS / GB）",
            "电池技术规范：IEC 62133、UL 2271/2272/2849、UN 38.3、GB/T 36972/38031、EN 50604",
            "充电安全 / 接口 / 通信协议：USB-C 通用充电器、智能充电 / V2G 互操作",
            "AI 与自动驾驶 / 功能安全：EU AI Act 实施细则、ISO 25119、IEC 61508、ISO 26262",
            "网络安全 / 软件 / OTA：EU CRA、UK PSTI、UN R155 / R156、NIST IoT、ETSI EN 303 645",
            "无线 / EMC / 频谱：EU RED 委托法规、FCC Part 15、日本 MIC、韩国 KCC、CISPR 14",
        ],
    },
    "PROD": {
        "name": "PROD 生产 / 供应链",
        "subtopics": [
            "物质限制：RoHS Annex II、REACH SVHC、Prop 65、J-MOSS、K-RoHS、PFAS 报告",
            "关键矿产：锂钴镍石墨供应链尽职调查（EU CSDDD、Battery Reg Article 49 等）",
            "工厂生产控制（FPC）/ 制造工艺合规",
            "数字产品护照（DPP）/ 电池护照原料披露",
            "供应链碳足迹（EU Battery Reg Article 7、ISO 14067）",
        ],
    },
    "CERT": {
        "name": "CERT 准入认证",
        "subtopics": [
            "型式认证 / 产品注册 / 合格评定（DoC）的程序变更",
            "强制认证：CCC / KC / PSE / UL / CE / UKCA",
            "经济运营商注册 / 生产者-进口商-经销商义务主体认定",
            "数字提交 / 简化路径 / 跨境互认协议",
        ],
    },
    # ── 售中 ───────────────────────────────────────────────────────────
    "IMPORT": {
        "name": "IMPORT 进口 / 流通",
        "subtopics": [
            "海关分类 / 进口许可 / HS Code 调整",
            "跨境电商监管（平台备案、清关流程）",
            "关税 / 反倾销税 / 出口管制 / 制裁名单",
            "危险品运输：IATA DGR、IMDG Code、ADR / RID、US DOT / PHMSA",
        ],
    },
    "RETAIL": {
        "name": "RETAIL 零售 / 销售合规",
        "subtopics": [
            "销售前年龄验证 / 资格验证 / 强制安全警示展示",
            "强制信息披露 / 标签（在售期间）",
            "广告限制 / 误导性宣传禁止 / 网红营销规范",
            "平台连带责任（电商平台 / 跨境零售）",
            "补贴 / 财政激励 / 以旧换新 / 能效标签（影响销售决策）",
        ],
    },
    "USE": {
        "name": "USE 消费者使用",
        "subtopics": [
            "消费者强制第三方责任险（RCA / MTPL）",
            "车辆登记 / 牌照 / 唯一识别 / VIN / 防伪标识",
            "驾照等级 / 年龄 / 头盔 / 反光装备 / PPE",
            "路权（人行道 / 自行车道）/ 限速 / 载客载重 / 改装禁令",
            "共享出行运营许可 / 电子围栏 / 强制停放区",
            "室内充电 / 公寓 / 停车场充电消防规则",
            "试点项目 / 上路许可 / 区域限制（trial schemes / pilot programs）",
        ],
    },
    # ── 售后 ───────────────────────────────────────────────────────────
    "ENFORCE": {
        "name": "ENFORCE 执法 / 监管行动",
        "subtopics": [
            "召回令 / 缺陷公告 / 强制召回（EU Safety Gate、CPSC、SAMR、ACCC）",
            "罚款决议 / 行政处罚",
            "市场监管行动 / 执法通告 / 违规清单",
            "违规改装查扣 / 强制下架",
        ],
    },
    "EOL": {
        "name": "EOL 回收 / 处置",
        "subtopics": [
            "EPR / WEEE / 电池回收义务、回收率目标",
            "押金返还制度 / 生产者责任登记",
            "报废处理流程 / 跨境废弃物转移（巴塞尔公约）",
            "责任险（产品责任 / 回收责任 / 反垄断 / 平台连带）",
        ],
    },
}

# L3 → L2 派生映射（reporter 端用于按 L2 分组显示）
DIM_TO_L2: dict[str, str] = {
    "RD":      "售前",
    "PROD":    "售前",
    "CERT":    "售前",
    "IMPORT":  "售中",
    "RETAIL":  "售中",
    "USE":     "售中",
    "ENFORCE": "售后",
    "EOL":     "售后",
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

# 单任务超时（秒）。需 > ai_client 内最大重试耗时：
#   retries=2 → 最多 3 次尝试，限流时 sleep 60s × 2 = 120s + 单次调用本身 ≈ 180s
CALL_TIMEOUT = 240

# 每议题双温度：低温捞确凿头部、高温捞长尾。
_FETCH_TEMPS = (0.2, 1.0)
_PLAN_TEMP   = 0.3   # 议题图要稳定但不过度收敛

# 并发数（保守值；Gemini Flash 限流时会自然降速）
_PLAN_WORKERS  = 4
_FETCH_WORKERS = 5
_AUDIT_WORKERS = 3

_db_lock = threading.Lock()


# ── 公共构造块 ────────────────────────────────────────────────────────────────

def _category_block() -> str:
    lines = [f"  • {p}" for p in CATEGORY_GROUPS["整机"]]
    lines.append("  ── 关键零部件 ──")
    lines.extend(f"  • {p}" for p in CATEGORY_GROUPS["零部件"])
    return "\n".join(lines)


def _subtopics_block(subtopics: list[str]) -> str:
    return "\n".join(f"  {i+1}. {s}" for i, s in enumerate(subtopics))


# ── Plan 阶段：议题图枚举 ─────────────────────────────────────────────────────

def _enumerate_topics(dim_id: str, quick: bool) -> list[dict]:
    """
    让模型枚举该维度下值得监控的议题清单。
    议题 = 监管面，跨年度稳定；具体法规由 fetch 阶段在议题下抓取。

    dim_id：八维 L3 代码之一（"RD" / "PROD" / "CERT" / "IMPORT" / "RETAIL" / "USE" / "ENFORCE" / "EOL"）。
    quick 参数当前不影响议题图，保留是为了将来需要时能差异化。
    返回 [{id, name, scope_hint}]，失败时 fallback 到 DIMENSIONS.subtopics。
    """
    _ = quick  # reserved
    today = datetime.now().strftime("%Y-%m-%d")
    dim   = DIMENSIONS[dim_id]

    prompt = prompts.load("researcher_plan").format(
        today=today,
        dim_lower=dim_id.lower(),
        dim_name=dim["name"],
        subtopics_block=_subtopics_block(dim["subtopics"]),
        category_block=_category_block(),
    )

    try:
        # 议题图不需要联网搜索——它是基于模型先验知识的"应监控面清单"。
        # 用 lite + 关 thinking：本任务是"按业务范围模板填 JSON 议题列表"，
        # 不需要复杂推理。flash + thinking 此处属于浪费。
        text = ai_client.call_json(
            prompt,
            system=prompts.load("researcher_plan_system").format(
                business_scope=_BUSINESS_SCOPE,
            ),
            temperature=_PLAN_TEMP,
            model="gemini-2.5-flash-lite",
            thinking_budget=0,
        )
        topics = parse_json_array(text) or []
    except Exception as e:
        _log.warning("PLAN %s failed: %s — fallback to subtopics", dim_id, e)
        topics = []

    valid: list[dict] = []
    seen_ids: set[str] = set()
    for t in topics:
        if not isinstance(t, dict):
            continue
        tid  = (t.get("id")   or "").strip()
        name = (t.get("name") or "").strip()
        if not tid or not name or tid in seen_ids:
            continue
        seen_ids.add(tid)
        valid.append({
            "id":         tid,
            "name":       name,
            "scope_hint": (t.get("scope_hint") or "").strip(),
        })

    if not valid:
        # Plan 失败兜底：把现有 subtopics 当议题用，至少不退化为零召回。
        valid = [
            {
                "id":         f"{dim_id.lower()}-fallback-{i+1}",
                "name":       s.split("：")[0][:20] if "：" in s else s[:20],
                "scope_hint": s,
            }
            for i, s in enumerate(dim["subtopics"])
        ]
        _log.info("PLAN %s using subtopics fallback (%d topics)", dim_id, len(valid))

    return valid


# ── Fetch 阶段：单议题召回 ─────────────────────────────────────────────────────

def _build_fetch_prompt(topic: dict, quick: bool, broaden: bool) -> str:
    """构造单议题搜索 prompt。broaden=True 时使用更宽时间窗 + 扩搜提示。"""
    today = datetime.now().strftime("%Y-%m-%d")

    multiplier    = _AUDIT_WINDOW_MULTIPLIER if broaden else 1
    new_days      = NEW_PUBLICATION_DAYS   * multiplier
    upcoming_days = UPCOMING_DEADLINE_DAYS * multiplier

    start_new = (datetime.now() - timedelta(days=new_days)).strftime("%Y-%m-%d")
    end_up    = (datetime.now() + timedelta(days=upcoming_days)).strftime("%Y-%m-%d")

    if quick:
        scope = prompts.load("researcher_scope_quick").format(
            new_days=new_days,
            start_new=start_new,
        )
    else:
        scope = prompts.load("researcher_scope_full").format(
            new_days=new_days,
            start_new=start_new,
            upcoming_days=upcoming_days,
            end_up=end_up,
        )

    audit_hint = (
        "[扩搜模式] 该议题在首轮搜索零命中。请扩大检索：\n"
        "  • 时间窗已自动延长（详见上文）\n"
        "  • 主动尝试本议题的次要法域、边缘机构、以前未覆盖的语言\n"
        "  • 公示稿、咨询期文件、地方性规则、即将到期的过渡条款均纳入\n"
        "  • 若确实无任何相关动态，输出空数组 []，不要凑数\n\n"
    ) if broaden else ""

    return prompts.load("researcher_fetch").format(
        today=today,
        topic_name=topic["name"],
        scope_hint=topic["scope_hint"] or "（无额外提示，按议题名展开）",
        scope=scope,
        audit_hint=audit_hint,
        category_block=_category_block(),
        output_schema=prompts.load("researcher_output_schema"),
    )


def _fetch_topic(
    topic: dict,
    quick: bool,
    temperature: float,
    *,
    broaden: bool = False,
) -> tuple[list, list[dict]]:
    """对单议题发起 grounded 搜索，返回 (regs, sources)。"""
    prompt = _build_fetch_prompt(topic, quick, broaden)
    text, sources = ai_client.call_grounded(
        prompt,
        system=prompts.load("researcher_system").format(
            business_scope=_BUSINESS_SCOPE,
        ),
        temperature=temperature,
    )
    regs = parse_json_array(text) or []
    return regs, sources


# ── 入库 ───────────────────────────────────────────────────────────────────────

# reg_id 垃圾值过滤——模型偶尔会塞占位文本，这里在入库前清掉。
# 真正的"语义规范化"（如 "(EU) 2024/2847" / "Reg 2024/2847" / "CRA" 视为同一编号）
# 留给 Stage 0 聚类阶段做。
_REG_ID_GARBAGE = {
    "", "null", "none", "n/a", "na", "tbd", "tba",
    "xxx", "yyy", "zzz", "placeholder", "未知", "无", "待定",
}


# 首页/索引页文件名——这些 URL 路径形式上"非空"但语义上等于 host-only
_INDEX_FILE_NAMES = {
    "index", "index.html", "index.htm", "index.php", "index.aspx",
    "default", "default.aspx", "default.html",
    "home", "home.html", "main", "main.html",
}


def _is_concrete_url(url: str | None) -> bool:
    """URL 必须指向具体内容——host-only 或仅含首页文件名的 URL 抓不到具体法规。

    第一性观察：法规页面 URL 必然有有意义的路径段（reg_id、文件名、唯一 slug）。
    没有路径 / 路径只是 'home'/'default.aspx' 类首页 → 抓回的是首页全文，
    AI 会拿首页 + 标题凭空发挥。
    """
    if not url:
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    path = (p.path or "").strip("/")
    if not path:
        return False
    segments = [s for s in path.split("/") if s]
    if len(segments) == 0:
        return False
    # 仅有 1 段且是已知首页文件名 → 等同于 host-only
    if len(segments) == 1 and segments[0].lower() in _INDEX_FILE_NAMES:
        return False
    return True


def _clean_reg_id(raw: str) -> str | None:
    """清洗模型自报的 reg_id；只过滤垃圾值，不做语义规范化。"""
    if not raw:
        return None
    s = raw.strip().strip("\"'`").strip()
    if not s or s.lower() in _REG_ID_GARBAGE:
        return None
    if len(s) < 3:
        return None
    # 含 XXX / TBD 占位文字符的视为脏数据
    if any(g in s.lower() for g in ("xxxx", "tbd", "placeholder")):
        return None
    return s


def _store(reg: dict, sources: list[dict]) -> bool:
    """保存一条发现到 raw_search_results；按 reg_hash(title) 跨表 30 天去重。"""
    title = (reg.get("title_original") or reg.get("title") or "").strip()
    if not title:
        return False

    h      = reg_hash(title)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()

    with _db_lock:
        with get_connection() as conn:
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

            title_cn       = (reg.get("title_cn") or "").strip()[:20]
            explicit_url   = (reg.get("url") or "").strip()
            grounding_urls = [
                s["url"] for s in sources
                if s.get("url") and "vertexaisearch" not in s["url"]
            ]

            # 候选池 = explicit_url + grounding 来源（去重保序）
            seen: set[str] = set()
            all_candidates: list[str] = []
            if explicit_url:
                seen.add(explicit_url)
                all_candidates.append(explicit_url)
            for u in grounding_urls:
                if u and u not in seen:
                    seen.add(u)
                    all_candidates.append(u)

            # URL 质量门：优先选指向具体内容的 URL，host-only / 首页文件名降级
            # 例：AI 给的 explicit_url='https://www.bmdv.bund.de/'（host-only），
            # 但 grounding 里有 https://www.bmdv.bund.de/SharedDocs/Pressemit/...html
            # → 后者优先；这样 scraper 抓到的就是具体页面而不是首页全文。
            concrete = [u for u in all_candidates if _is_concrete_url(u)]
            if concrete:
                rep_url   = concrete[0]
                fallbacks = [u for u in all_candidates if u != rep_url]
            elif all_candidates:
                # 全是 host-only：留主 URL 但下游 scraper 大概率拒收 → fallback 接管
                rep_url   = all_candidates[0]
                fallbacks = all_candidates[1:]
            else:
                rep_url = ""
                fallbacks = []
            fallback_json = json.dumps(fallbacks[:5], ensure_ascii=False) if fallbacks else None

            market_hint = (reg.get("market_hint") or "").strip()
            relevance   = (reg.get("relevance_note") or "").strip()

            # 模型自报的法规编号——下游 Stage 0 聚类的主输入。
            # 过滤明显垃圾值（空、占位文本、纯数字 < 3 位等）。
            reg_id_raw = (reg.get("reg_id") or "").strip()
            reg_id     = _clean_reg_id(reg_id_raw)

            conn.execute("""
                INSERT OR IGNORE INTO raw_search_results
                    (query_date, source_url, title, title_cn, snippet, priority,
                     product_category, market, content_hash, scrape_status,
                     fallback_urls, reg_id)
                VALUES (?,?,?,?,?,'高',null,?,?,'待抓取',?,?)
            """, (
                datetime.now().isoformat(),
                rep_url, title, title_cn, relevance[:500], market_hint,
                h, fallback_json, reg_id,
            ))
            return bool(conn.execute("SELECT changes()").fetchone()[0])


# ── 主入口：三阶段编排 ────────────────────────────────────────────────────────

def run_research(quick: bool = False) -> tuple[int, int]:
    """
    三阶段：Plan（议题图）→ Fetch（双温度召回）→ Audit（零命中补查）。
    返回 (inserted, skipped) 与旧版签名兼容。
    """
    init_db()

    # 种子注入（保留旧逻辑，作为占位去重 + 黄金集标记）
    from seeds import inject_seeds, SEEDS
    seed_new, seed_refreshed = inject_seeds()
    print(f"\n  种子库：{len(SEEDS)} 条 → 新增 {seed_new}，刷新 {seed_refreshed}")

    mode = "快速（仅新发布）" if quick else "全量（新发布 + 即将生效）"
    print(f"\n  模式：{mode}")

    # ── 阶段 1：Plan ───────────────────────────────────────────────────────────
    print(f"\n  ── 阶段 1/3：议题图枚举 ──\n")
    dim_topics: dict[int, list[dict]] = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_PLAN_WORKERS, thread_name_prefix="plan",
    ) as ex:
        plan_futs = {
            ex.submit(_enumerate_topics, dim_id, quick): dim_id
            for dim_id in DIMENSIONS
        }
        for fut in concurrent.futures.as_completed(plan_futs):
            dim_id = plan_futs[fut]
            try:
                topics = fut.result(timeout=CALL_TIMEOUT)
            except Exception as e:
                _log.error("PLAN FAIL %s: %s", dim_id, e)
                topics = []
            dim_topics[dim_id] = topics
            dim_name = DIMENSIONS[dim_id]["name"]
            print(f"  {dim_id} {dim_name}：{len(topics)} 议题")
            for t in topics:
                print(f"        • [{t['id']}] {t['name']}")
            _log.info("PLAN %s topics=%d ids=%s",
                      dim_id, len(topics), [t["id"] for t in topics])

    # 议题平铺为 fetch 任务（每议题 × 每温度 一次调用）
    fetch_tasks: list[tuple[int, dict, float]] = [
        (dim_id, topic, t)
        for dim_id, topics in dim_topics.items()
        for topic in topics
        for t in _FETCH_TEMPS
    ]

    # ── 阶段 2：Fetch（双温度并行）─────────────────────────────────────────────
    total_fetch  = len(fetch_tasks)
    total_topics = sum(len(t) for t in dim_topics.values())
    print(f"\n  ── 阶段 2/3：议题召回（{total_topics} 议题 × {len(_FETCH_TEMPS)} 温度 = {total_fetch} 调用）──\n")

    inserted = skipped = failed = 0
    topic_hits: dict[str, int] = {tp["id"]: 0
                                  for tps in dim_topics.values() for tp in tps}
    completed = 0

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_FETCH_WORKERS, thread_name_prefix="fetch",
    ) as ex:
        fut_meta = {
            ex.submit(_fetch_topic, topic, quick, temp): (dim_id, topic, temp)
            for (dim_id, topic, temp) in fetch_tasks
        }
        for fut in concurrent.futures.as_completed(fut_meta):
            dim_id, topic, temp = fut_meta[fut]
            completed += 1
            label = f"{dim_id}/{topic['id']}/t={temp}"
            try:
                regs, srcs = fut.result(timeout=CALL_TIMEOUT)
                n_in = n_sk = 0
                for reg in regs:
                    if isinstance(reg, dict) and _store(reg, srcs):
                        n_in += 1
                    else:
                        n_sk += 1
                inserted += n_in
                skipped  += n_sk
                topic_hits[topic["id"]] += n_in
                _log.info("FETCH %s regs=%d new=%d dup=%d", label, len(regs), n_in, n_sk)
                print(f"  [{completed:>3}/{total_fetch}] {label}：{len(regs)} 条 → 入库 {n_in}，重复 {n_sk}")
            except (TimeoutError, concurrent.futures.TimeoutError):
                failed += 1
                _log.warning("FETCH TIMEOUT %s", label)
                print(f"  [{completed:>3}/{total_fetch}] {label}：⏱ 超时")
            except Exception as e:
                failed += 1
                _log.error("FETCH FAIL %s: %s", label, e)
                print(f"  [{completed:>3}/{total_fetch}] {label}：❌ {e}")

    # ── 阶段 3：Audit（零命中议题扩搜）─────────────────────────────────────────
    zero_hit_pairs: list[tuple[int, dict]] = [
        (dim_id, topic)
        for dim_id, topics in dim_topics.items()
        for topic in topics
        if topic_hits.get(topic["id"], 0) == 0
    ]

    if not zero_hit_pairs:
        print(f"\n  ── 阶段 3/3：所有 {total_topics} 议题至少一次命中，跳过补查 ──")
    else:
        print(f"\n  ── 阶段 3/3：零命中议题补查（{len(zero_hit_pairs)}/{total_topics} 议题）──\n")
        recovered = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_AUDIT_WORKERS, thread_name_prefix="audit",
        ) as ex:
            fut_meta2 = {
                ex.submit(_fetch_topic, topic, quick, 1.0, broaden=True): (dim_id, topic)
                for (dim_id, topic) in zero_hit_pairs
            }
            for fut in concurrent.futures.as_completed(fut_meta2):
                dim_id, topic = fut_meta2[fut]
                label = f"{dim_id}/{topic['id']}"
                try:
                    regs, srcs = fut.result(timeout=CALL_TIMEOUT)
                    n_in = 0
                    for reg in regs:
                        if isinstance(reg, dict) and _store(reg, srcs):
                            n_in += 1
                    inserted  += n_in
                    recovered += n_in
                    _log.info("AUDIT %s regs=%d recovered=%d", label, len(regs), n_in)
                    print(f"  [audit] {label}：{len(regs)} 条 → 补回 {n_in}")
                except Exception as e:
                    _log.warning("AUDIT FAIL %s: %s", label, e)
                    print(f"  [audit] {label}：❌ {e}")
        print(f"\n  补查完成：{recovered} 条额外法规进入流水线")

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    suffix = f"  （{failed} 个调用失败）" if failed else ""
    print(f"\n  总计：入库 {inserted} 条待抓取，重复跳过 {skipped} 条{suffix}")
    return inserted, skipped


if __name__ == "__main__":
    import sys
    run_research(quick="--quick" in sys.argv)
