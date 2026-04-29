"""
数据库层：连接管理、schema 创建/迁移、读写助手。

去重契约：
  raw_search_results.content_hash    = reg_hash(title)
  compliance_analysis.content_hash   = reg_hash(title)
两者用同一函数（utils.reg_hash），保证跨表/跨阶段去重一致。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from config import DATABASE_PATH
from utils import get_logger

SCHEMA_VERSION = 9
_log = get_logger("database")


@contextmanager
def get_connection():
    """打开 sqlite 连接；遇到 iCloud 同步占用时按 1/2/4s 退避重试 3 次。"""
    conn: sqlite3.Connection | None = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=30)
            break
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "unable to open" in str(exc) and attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    if conn is None:
        raise last_exc  # type: ignore[misc]
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL_LATEST = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS raw_search_results (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        query_date       TEXT NOT NULL,
        source_url       TEXT,
        title            TEXT,
        title_cn         TEXT,
        snippet          TEXT,
        priority         TEXT,
        product_category TEXT,
        market           TEXT,
        raw_text         TEXT,
        content_hash     TEXT UNIQUE,
        scrape_status    TEXT NOT NULL DEFAULT '待抓取'
                         CHECK(scrape_status IN ('待抓取','已抓取','失败','需人工')),
        fallback_urls    TEXT,
        reg_id           TEXT,
        consolidated_into INTEGER REFERENCES raw_search_results(id)
    );

    CREATE TABLE IF NOT EXISTS scraped_content (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_id       INTEGER NOT NULL REFERENCES raw_search_results(id),
        full_text    TEXT,
        content_type TEXT NOT NULL DEFAULT 'unknown'
                     CHECK(content_type IN ('webpage','pdf','unknown')),
        scrape_date  TEXT NOT NULL,
        word_count   INTEGER,
        truncated    INTEGER NOT NULL DEFAULT 0,
        ai_analyzed  INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS compliance_analysis (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        scraped_id                  INTEGER NOT NULL REFERENCES scraped_content(id),
        compliance_requirement      TEXT,
        compliance_deadline         TEXT,
        key_dates                   TEXT,
        action_items                TEXT,
        impact_level                TEXT CHECK(impact_level IN ('🔴','🟡','🟢')),
        affected_products           TEXT,
        affected_products_display   TEXT,
        affected_markets            TEXT,
        market_tier                 INTEGER,
        worst_case_scenario         TEXT,
        business_impact             TEXT,
        business_dimensions         TEXT,
        sources                     TEXT,
        content_hash                TEXT,
        source_institution          TEXT,
        source_language             TEXT,
        analysis_date               TEXT NOT NULL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_content_hash
        ON compliance_analysis (content_hash)
        WHERE content_hash IS NOT NULL;
"""


def init_db() -> None:
    with get_connection() as conn:
        _bootstrap_or_migrate(conn)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _bootstrap_or_migrate(conn: sqlite3.Connection) -> None:
    fresh = not _table_exists(conn, "raw_search_results")

    conn.executescript(_DDL_LATEST)

    if fresh:
        conn.execute("INSERT OR REPLACE INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        return

    row = conn.execute("SELECT version FROM schema_version").fetchone()
    cur_v = row[0] if row else 0

    if cur_v >= SCHEMA_VERSION:
        return

    if cur_v < 3:
        _migrate_to_v3(conn)
    if cur_v < 4:
        _migrate_to_v4(conn)
    if cur_v < 5:
        _migrate_to_v5(conn)
    if cur_v < 6:
        _migrate_to_v6(conn)
    if cur_v < 7:
        _migrate_to_v7(conn)
    if cur_v < 8:
        _migrate_to_v8(conn)
    if cur_v < 9:
        _migrate_to_v9(conn)

    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v0→v3：补齐历史新增列、emoji 化 impact_level、规范化产品名。"""
    for table, col_def in (
        ("compliance_analysis", "key_dates TEXT"),
        ("compliance_analysis", "business_impact TEXT"),
        ("compliance_analysis", "sources TEXT"),
        ("compliance_analysis", "content_hash TEXT"),
        ("compliance_analysis", "affected_products_display TEXT"),
        ("compliance_analysis", "market_tier INTEGER"),
        ("compliance_analysis", "source_institution TEXT"),
        ("compliance_analysis", "source_language TEXT"),
        ("raw_search_results",  "title_cn TEXT"),
    ):
        col_name = col_def.split()[0]
        if not _column_exists(conn, table, col_name):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except sqlite3.OperationalError as e:
                _log.warning("ALTER %s.%s skipped: %s", table, col_name, e)

    _migrate_importance_emoji(conn)
    _migrate_product_names(conn)


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """v3→v4：scraped_content 添加 truncated 字段。"""
    if not _column_exists(conn, "scraped_content", "truncated"):
        conn.execute(
            "ALTER TABLE scraped_content ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """v4→v5：raw_search_results 添加 fallback_urls 字段（JSON 数组，scraper 主 URL 失败时按序回退）。"""
    if not _column_exists(conn, "raw_search_results", "fallback_urls"):
        conn.execute("ALTER TABLE raw_search_results ADD COLUMN fallback_urls TEXT")


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """v5→v6：raw_search_results 添加 reg_id 字段（模型自报的法规规范编号，用于 Stage 0 跨条目聚类）。"""
    if not _column_exists(conn, "raw_search_results", "reg_id"):
        conn.execute("ALTER TABLE raw_search_results ADD COLUMN reg_id TEXT")


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """v6→v7：raw_search_results 添加 consolidated_into（Stage 0 软合并指针）。

    被合并的条目设 consolidated_into=主条目id，scrape_status 改为 '已抓取'，
    scraper 与 analyzer 都跳过；保留行便于复盘"模型在哪些 reg_id 上重复"。
    """
    if not _column_exists(conn, "raw_search_results", "consolidated_into"):
        conn.execute(
            "ALTER TABLE raw_search_results "
            "ADD COLUMN consolidated_into INTEGER REFERENCES raw_search_results(id)"
        )


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """v7→v8：compliance_analysis 添加 business_dimensions（八维 L3 业务影响坐标）。

    JSON 数组，元素来自 enum：RD / PROD / CERT / IMPORT / RETAIL / USE / ENFORCE / EOL。
    任一维度触发即"相关"；空数组表示真"不相关"。
    用途：搜索切片（researcher 按 8 维 L3 枚举议题）+ 收敛分组（Stage 3 Pass 2）。
    报告端有意保持精简，不展示该字段——维度信号通过 business_impact 文本间接传达。
    """
    if not _column_exists(conn, "compliance_analysis", "business_dimensions"):
        conn.execute(
            "ALTER TABLE compliance_analysis ADD COLUMN business_dimensions TEXT"
        )


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """v8→v9：清理 raw_search_results 的死列。

    publish_date / effective_date 自始至终没有写入路径——researcher、scraper、analyzer
    全都不写。日期信息由 analyzer 写入 compliance_analysis.key_dates JSON。
    SQLite 3.35+ 支持 ALTER TABLE DROP COLUMN，本项目部署机器为 3.50。
    """
    for col in ("publish_date", "effective_date"):
        if _column_exists(conn, "raw_search_results", col):
            try:
                conn.execute(f"ALTER TABLE raw_search_results DROP COLUMN {col}")
            except sqlite3.OperationalError as e:
                _log.warning("DROP COLUMN raw_search_results.%s skipped: %s", col, e)


def _migrate_importance_emoji(conn: sqlite3.Connection) -> None:
    """impact_level 由 高/中/低 → 🔴/🟡/🟢，并去掉旧 CHECK 约束。"""
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='compliance_analysis'"
    ).fetchone()
    if not sql_row or "'高'" not in (sql_row[0] or ""):
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE compliance_analysis_new (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            scraped_id                  INTEGER NOT NULL REFERENCES scraped_content(id),
            compliance_requirement      TEXT,
            compliance_deadline         TEXT,
            key_dates                   TEXT,
            action_items                TEXT,
            impact_level                TEXT CHECK(impact_level IN ('🔴','🟡','🟢')),
            affected_products           TEXT,
            affected_products_display   TEXT,
            affected_markets            TEXT,
            market_tier                 INTEGER,
            worst_case_scenario         TEXT,
            business_impact             TEXT,
            sources                     TEXT,
            content_hash                TEXT,
            source_institution          TEXT,
            source_language             TEXT,
            analysis_date               TEXT NOT NULL
        );

        INSERT INTO compliance_analysis_new (
            id, scraped_id, compliance_requirement, compliance_deadline,
            key_dates, action_items, impact_level,
            affected_products, affected_products_display,
            affected_markets, market_tier,
            worst_case_scenario, business_impact, sources, content_hash,
            source_institution, source_language, analysis_date
        )
        SELECT
            id, scraped_id, compliance_requirement, compliance_deadline,
            key_dates, action_items,
            CASE impact_level
                WHEN '高' THEN '🔴'
                WHEN '中' THEN '🟡'
                WHEN '低' THEN '🟢'
                ELSE impact_level
            END,
            affected_products, affected_products_display,
            affected_markets, market_tier,
            worst_case_scenario, business_impact, sources, content_hash,
            source_institution, source_language, analysis_date
        FROM compliance_analysis;

        DROP TABLE compliance_analysis;
        ALTER TABLE compliance_analysis_new RENAME TO compliance_analysis;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_content_hash
            ON compliance_analysis (content_hash)
            WHERE content_hash IS NOT NULL;
    """)
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_product_names(conn: sqlite3.Connection) -> None:
    """产品名规范化到 5 类整机；旧名映射 + 重置 affected_products_display。"""
    _OLD_TO_NEW = {
        "Ebike":          "电助力自行车",
        "共享电动滑板车": "电动滑板车",
        "电动轻型摩托车": "电动摩托车",
    }
    rows = conn.execute(
        "SELECT id, affected_products FROM compliance_analysis"
    ).fetchall()
    updated = 0
    for row in rows:
        raw = row["affected_products"] or ""
        if not any(old in raw for old in _OLD_TO_NEW):
            continue
        parts = [p.strip() for p in raw.split("、") if p.strip()]
        new_parts: list[str] = []
        seen: set[str] = set()
        for p in parts:
            mapped = _OLD_TO_NEW.get(p, p)
            if mapped not in seen:
                new_parts.append(mapped)
                seen.add(mapped)
        new_val = "、".join(new_parts) if new_parts else raw
        conn.execute(
            "UPDATE compliance_analysis "
            "SET affected_products = ?, affected_products_display = NULL WHERE id = ?",
            (new_val, row["id"]),
        )
        updated += 1
    if updated:
        _log.info("产品名迁移：%d 条记录已更新", updated)


# ── raw_search_results ────────────────────────────────────────────────────────


def get_raw_result(raw_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM raw_search_results WHERE id = ?", (raw_id,)
        ).fetchone()


def get_pending_scrape() -> list[sqlite3.Row]:
    """待抓取条目；自动跳过 Stage 0 软合并的从条目（consolidated_into 非空）。"""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM raw_search_results "
            "WHERE scrape_status = '待抓取' AND consolidated_into IS NULL"
        ).fetchall()


def update_scrape_status(raw_id: int, status: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE raw_search_results SET scrape_status = ? WHERE id = ?",
            (status, raw_id),
        )


# ── scraped_content ───────────────────────────────────────────────────────────


def insert_scraped_content(data: dict) -> int:
    sql = """
        INSERT INTO scraped_content
            (raw_id, full_text, content_type, scrape_date, word_count, truncated, ai_analyzed)
        VALUES
            (:raw_id, :full_text, :content_type, :scrape_date, :word_count, :truncated, :ai_analyzed)
    """
    data.setdefault("scrape_date", datetime.now().isoformat())
    data.setdefault("content_type", "unknown")
    data.setdefault("ai_analyzed", 0)
    data.setdefault("truncated", 0)
    data.setdefault(
        "word_count",
        len(data.get("full_text", "").split()) if data.get("full_text") else 0,
    )
    with get_connection() as conn:
        cur = conn.execute(sql, data)
        return cur.lastrowid


def get_unanalyzed_content() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM scraped_content WHERE ai_analyzed = 0"
        ).fetchall()


def mark_analyzed(scraped_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE scraped_content SET ai_analyzed = 1 WHERE id = ?", (scraped_id,)
        )


# ── compliance_analysis：报表查询 ──────────────────────────────────────────────

_REPORT_SELECT = """
    SELECT
        ca.impact_level,
        rs.title,
        rs.title_cn,
        ca.compliance_requirement,
        COALESCE(ca.affected_products_display, ca.affected_products) AS affected_products_display,
        ca.affected_markets,
        ca.compliance_deadline,
        ca.key_dates,
        rs.source_url,
        rs.fallback_urls,
        rs.market,
        ca.worst_case_scenario,
        ca.business_impact,
        ca.sources,
        ca.analysis_date,
        ca.source_institution,
        ca.source_language,
        CASE WHEN sc.full_text LIKE '[Gemini synthesis]%' THEN 1 ELSE 0 END AS is_synth
    FROM compliance_analysis ca
    JOIN scraped_content      sc ON sc.id = ca.scraped_id
    JOIN raw_search_results   rs ON rs.id = sc.raw_id
    WHERE COALESCE(ca.affected_products_display, ca.affected_products) != '不相关'
      AND COALESCE(ca.affected_products_display, ca.affected_products) IS NOT NULL
      AND COALESCE(ca.affected_products_display, ca.affected_products) != ''
      AND rs.consolidated_into IS NULL
"""

_REPORT_ORDER = """
    ORDER BY
        CASE ca.impact_level WHEN '🔴' THEN 1 WHEN '🟡' THEN 2 WHEN '🟢' THEN 3 ELSE 9 END,
        COALESCE(ca.market_tier, 99),
        COALESCE(ca.affected_markets, ''),
        CASE
            WHEN COALESCE(ca.affected_products_display, ca.affected_products) LIKE '%短交通%' THEN 1
            WHEN COALESCE(ca.affected_products_display, ca.affected_products) LIKE '%ebike%'
              OR COALESCE(ca.affected_products_display, ca.affected_products) LIKE '%Ebike%' THEN 2
            WHEN COALESCE(ca.affected_products_display, ca.affected_products) LIKE '%电摩%' THEN 3
            WHEN COALESCE(ca.affected_products_display, ca.affected_products) LIKE '%割草机%' THEN 4
            ELSE 9
        END
"""


def get_all_analyses() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(_REPORT_SELECT + _REPORT_ORDER).fetchall()


def get_week_analyses(since: str) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            _REPORT_SELECT + "  AND ca.analysis_date >= ?\n" + _REPORT_ORDER,
            (since,),
        ).fetchall()


def get_manual_followup() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("""
            SELECT title, source_url, query_date
            FROM raw_search_results
            WHERE scrape_status = '需人工'
            ORDER BY query_date DESC
        """).fetchall()


# ── 孤儿清理（被合并/删除分析后，回收对应的 scraped_content）──────────────────


def delete_orphan_scraped() -> int:
    """删除没有任何 compliance_analysis 引用的 scraped_content。

    安全护栏：当 compliance_analysis 完全为空时直接返回 0，避免
    `NOT IN ()` 在 SQL 中等价于 TRUE 而把整张表清空。
    """
    with get_connection() as conn:
        n_analyses = conn.execute(
            "SELECT COUNT(*) FROM compliance_analysis"
        ).fetchone()[0]
        if not n_analyses:
            return 0
        cur = conn.execute("""
            DELETE FROM scraped_content
            WHERE id NOT IN (SELECT DISTINCT scraped_id FROM compliance_analysis)
        """)
        return cur.rowcount or 0


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at: {DATABASE_PATH}")
    with get_connection() as conn:
        v = conn.execute("SELECT version FROM schema_version").fetchone()
        print(f"  schema_version = {v[0] if v else '(missing)'}")
