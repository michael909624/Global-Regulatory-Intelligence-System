#!/usr/bin/env python3
"""
GRIS 命令行入口
用法：python gris.py <命令> [参数]
"""
import sys


# ── 帮助文本 ──────────────────────────────────────────────────────────────────

HELP = """
╔══════════════════════════════════════════════════════════════╗
║              GRIS — 全球法规情报系统                         ║
╚══════════════════════════════════════════════════════════════╝

用法：python gris.py <命令> [参数]

━━━  主流水线  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  run               完整流水线：发现 → 抓取 → 分析 → 报告（4步）
  run --quick       快速模式：仅搜索近 90 天新发布

  research          Gemini 发现层：搜索并入库待抓取 URL（D1–D4 共 4 次调用）
  research --quick  快速模式（仅新发布窗口）

  scrape            抓取待处理 URL 的网页/PDF 原文
  analyze           对已抓取内容进行 AI 合规分析（基于真实原文）
  report            生成 Excel 周报（保存至 reports/ 目录）

━━━  辅助工具  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  view              终端查看最近分析结果（默认 20 条）
                      view 高       只看高影响（🔴）
                      view 中       只看中影响（🟡）
                      view 低       只看低影响（🟢）
                      view 高 50    高影响，最多 50 条

  status            显示数据库统计概览

  backfill          为缺失 source_url 的历史条目调用 Gemini 补全官方链接

  manual            人工补录：逐条粘贴抓取失败页面的正文

  retry             将失败/需人工的 URL 重置为待抓取，重新交给 scraper

  clean             清除「不相关」分析记录，保持数据库整洁

  reanalyze         重置「不相关」条目并立即重新分析

  reset             清空数据（带确认提示）
                      无参数    → 清空全部（数据库 + 报告）
                      --db      → 仅清空数据库
                      --reports → 仅删除报告文件

  init              初始化数据库（首次使用时运行）

  seed              注入种子源（已知权威源 URL 列表，保底召回）
  evaluate          对照 tests/gold_set.json 黄金集计算召回率

━━━  典型工作流  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  每周深度扫描：    python3 gris.py run
  日常快速扫描：    python3 gris.py run --quick
  单独研究步骤：    python3 gris.py research
  仅生成报告：      python3 gris.py report
  查看最新结果：    python3 gris.py view 高
  只看状态：        python3 gris.py status
  重置所有数据：    python3 gris.py reset

  交互菜单：        python3 gris.py
"""


# ── 命令实现 ──────────────────────────────────────────────────────────────────

def cmd_research(args: list[str]):
    import researcher
    quick = "--quick" in args
    researcher.run_research(quick=quick)


def cmd_backfill(args: list[str]):
    """Ask Gemini to fill in missing source URLs for existing DB entries."""
    import time
    from database import get_connection, init_db
    import ai_client

    init_db()

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT rs.id, rs.title, ca.affected_markets
            FROM compliance_analysis ca
            JOIN scraped_content sc ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            WHERE (rs.source_url IS NULL OR rs.source_url = '')
        """).fetchall()

    if not rows:
        print("所有条目已有来源 URL，无需补全。")
        return

    print(f"共 {len(rows)} 条缺少来源 URL，开始 Gemini 补全...\n")
    updated = 0
    backfill_system = "你是法规链接补全助手,只输出最权威的官方原文 URL,不要解释。"

    for i, (raw_id, title, markets) in enumerate(rows, 1):
        prompt = (
            f"法规名称：{title}\n"
            f"适用市场：{markets or '未知'}\n\n"
            "请给出该法规最权威的官方原文链接（官方公报/标准机构/政府网站）。\n"
            "只输出 URL 本身，不含任何解释。若无法确认则输出 null。"
        )
        try:
            text, _ = ai_client.call_grounded(
                prompt,
                system=backfill_system,
                temperature=0.0,
                top_p=None,
                return_sources=False,
            )
            url = text.strip().strip('"').strip("'")
            if url and url.lower() != "null" and url.startswith("http"):
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE raw_search_results SET source_url=? WHERE id=?",
                        (url, raw_id),
                    )
                updated += 1
                print(f"  [{i:>3}/{len(rows)}] ✓  {title[:50]}")
                print(f"            {url}")
            else:
                print(f"  [{i:>3}/{len(rows)}] –  {title[:50]}  (无法确认)")
        except Exception as e:
            print(f"  [{i:>3}/{len(rows)}] ❌  {title[:50]}  ({e})")

        if i < len(rows):
            time.sleep(2)

    print(f"\n补全完成：{updated}/{len(rows)} 条已更新来源 URL。")
    print("运行 [report] 重新生成报告。")


def cmd_run(args: list[str]):
    import time
    from database import get_connection
    import researcher
    import scraper
    import analyzer
    import reporter

    quick = "--quick" in args

    def _elapsed(t0: float) -> str:
        s = time.time() - t0
        return f"{s:.1f}s" if s < 60 else f"{int(s)//60}m{int(s)%60:02d}s"

    print("=" * 44)
    print("           GRIS 启动")
    print("=" * 44)
    results = {}

    mode_label = "快速（仅新发布）" if quick else "全量（新发布 + 即将生效）"

    print(f"\n[1/4] Gemini 发现层（{mode_label}）...")
    t0 = time.time()
    try:
        inserted, skipped = researcher.run_research(quick=quick)
        print(f"      完成 ({_elapsed(t0)})  入库 {inserted} 条待抓取，重复 {skipped} 条")
        results["发现"] = (True, f"入库 {inserted} 条")
    except Exception as e:
        print(f"      ❌ 错误 ({_elapsed(t0)})：{e}")
        results["发现"] = (False, str(e))

    print("\n[2/4] 抓取法规原文...")
    t0 = time.time()
    try:
        ok, fail, manual = scraper.scrape_all()
        print(f"      完成 ({_elapsed(t0)})")
        results["抓取"] = (True, f"成功 {ok}，需人工 {fail + manual}")
    except Exception as e:
        print(f"      ❌ 错误 ({_elapsed(t0)})：{e}")
        results["抓取"] = (False, str(e))

    print("\n[3/4] 合规分析（基于抓取原文）...")
    t0 = time.time()
    try:
        n_analyzed, n_dup, n_fail = analyzer.run_analysis()
        print(f"      完成 ({_elapsed(t0)})  分析 {n_analyzed} 条，重复 {n_dup}，失败 {n_fail}")
        results["分析"] = (True, f"分析 {n_analyzed} 条")
    except Exception as e:
        print(f"      ❌ 错误 ({_elapsed(t0)})：{e}")
        results["分析"] = (False, str(e))

    print("\n[4/4] 生成周报...")
    t0 = time.time()
    report_path = None
    try:
        report_path = reporter.generate_report()
        print(f"      完成 ({_elapsed(t0)})")
        results["报告"] = (True, report_path)
    except Exception as e:
        print(f"      ❌ 错误 ({_elapsed(t0)})：{e}")
        results["报告"] = (False, str(e))

    with get_connection() as conn:
        total_analyzed = conn.execute("SELECT COUNT(*) FROM compliance_analysis").fetchone()[0]

    print("\n" + "=" * 44)
    print("           运行汇总")
    print("=" * 44)
    print(f"  法规总量        {total_analyzed:>5} 条")
    print()
    for step, (ok, detail) in results.items():
        print(f"  [{'✓' if ok else '✗'}] {step}：{detail}")
    if report_path:
        print(f"\n  报告已保存：{report_path}")
    print("=" * 44)


def cmd_scrape(_args: list[str]):
    import scraper
    scraper.scrape_all()


def cmd_analyze(_args: list[str]):
    import analyzer
    analyzer.run_analysis()


def cmd_report(_args: list[str]):
    import reporter
    reporter.generate_report()


def cmd_view(args: list[str]):
    from database import get_connection, init_db

    init_db()

    _IMPACT_MAP = {"高": "🔴", "中": "🟡", "低": "🟢"}
    _IMPACT_TAG = {"🔴": "🔴高", "🟡": "🟡中", "🟢": "🟢低"}

    impact_filter = None
    limit = 20
    for a in args:
        if a in _IMPACT_MAP:
            impact_filter = _IMPACT_MAP[a]
        elif a in _IMPACT_MAP.values():
            impact_filter = a
        elif a.isdigit():
            limit = int(a)

    if impact_filter:
        sql = """
            SELECT ca.impact_level, ca.compliance_deadline, ca.affected_markets,
                   ca.affected_products, ca.compliance_requirement, ca.action_items,
                   rs.title, rs.source_url, rs.market, ca.analysis_date
            FROM compliance_analysis ca
            JOIN scraped_content sc    ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            WHERE ca.impact_level = ?
            ORDER BY ca.id DESC
            LIMIT ?
        """
        params = (impact_filter, limit)
    else:
        sql = """
            SELECT ca.impact_level, ca.compliance_deadline, ca.affected_markets,
                   ca.affected_products, ca.compliance_requirement, ca.action_items,
                   rs.title, rs.source_url, rs.market, ca.analysis_date
            FROM compliance_analysis ca
            JOIN scraped_content sc    ON sc.id = ca.scraped_id
            JOIN raw_search_results rs ON rs.id = sc.raw_id
            ORDER BY ca.id DESC
            LIMIT ?
        """
        params = (limit,)

    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        label = f"「{impact_filter}」" if impact_filter else ""
        print(f"没有{label}分析记录。")
        return

    label_map = {"🔴": "高", "🟡": "中", "🟢": "低"}
    label = f"（仅限{label_map.get(impact_filter, impact_filter)}影响）" if impact_filter else ""
    print(f"\n最近 {len(rows)} 条分析结果{label}：\n")
    print("─" * 72)
    for row in rows:
        tag      = _IMPACT_TAG.get(row["impact_level"], row["impact_level"] or "[?]")
        title    = (row["title"] or "无标题")[:55]
        deadline = row["compliance_deadline"] or "无截止"
        markets  = row["affected_markets"] or row["market"] or "—"
        products = row["affected_products"] or "—"
        req      = (row["compliance_requirement"] or "")[:90]
        url      = row["source_url"] or ""
        date     = (row["analysis_date"] or "")[:10]

        print(f"{tag}  {title}")
        print(f"     截止 {deadline:<12}  市场 {markets}")
        print(f"     产品 {products}")
        print(f"     {req}")
        print(f"     {url}  ({date})")
        print("─" * 72)


def cmd_retry(_args: list[str]):
    from database import get_connection, init_db

    init_db()
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM raw_search_results WHERE scrape_status IN ('失败','需人工')"
        ).fetchone()[0]
        if count == 0:
            print("没有失败或需人工的记录，无需重试。")
            return
        conn.execute(
            "UPDATE raw_search_results SET scrape_status='待抓取' "
            "WHERE scrape_status IN ('失败','需人工')"
        )
    print(f"已将 {count} 条记录重置为待抓取。运行 [scrape] 重新尝试。")


def cmd_clean(_args: list[str]):
    from database import get_connection, init_db

    init_db()
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM compliance_analysis WHERE affected_products='不相关'"
        ).fetchone()[0]

    if count == 0:
        print("没有「不相关」记录，无需清理。")
        return

    print(f"找到 {count} 条 affected_products='不相关' 的分析记录。")
    ans = input("确认删除？[y/N] ").strip().lower()
    if ans != "y":
        print("已取消。")
        return

    with get_connection() as conn:
        conn.execute("DELETE FROM compliance_analysis WHERE affected_products='不相关'")
    print(f"已删除 {count} 条不相关记录。")


def cmd_reanalyze(_args: list[str]):
    """重置「不相关」条目并重新分析。"""
    from database import init_db
    from analyzer import requeue_irrelevant, run_analysis

    init_db()
    n = requeue_irrelevant()
    if n > 0:
        print(f"\n  开始重新分析 {n} 条条目…\n")
        run_analysis()


def cmd_manual(_args: list[str]):
    import manual_input
    manual_input.run()


def cmd_status(_args: list[str]):
    from database import get_connection, init_db

    init_db()

    with get_connection() as conn:
        total_raw   = conn.execute("SELECT COUNT(*) FROM raw_search_results").fetchone()[0]
        scraped     = conn.execute("SELECT COUNT(*) FROM raw_search_results WHERE scrape_status='已抓取'").fetchone()[0]
        pending     = conn.execute("SELECT COUNT(*) FROM raw_search_results WHERE scrape_status='待抓取'").fetchone()[0]
        failed      = conn.execute("SELECT COUNT(*) FROM raw_search_results WHERE scrape_status='失败'").fetchone()[0]
        manual      = conn.execute("SELECT COUNT(*) FROM raw_search_results WHERE scrape_status='需人工'").fetchone()[0]
        analyzed    = conn.execute("SELECT COUNT(*) FROM compliance_analysis").fetchone()[0]
        high_impact = conn.execute("SELECT COUNT(*) FROM compliance_analysis WHERE impact_level='🔴'").fetchone()[0]
        mid_impact  = conn.execute("SELECT COUNT(*) FROM compliance_analysis WHERE impact_level='🟡'").fetchone()[0]
        low_impact  = conn.execute("SELECT COUNT(*) FROM compliance_analysis WHERE impact_level='🟢'").fetchone()[0]

    print()
    print("━━━  数据库概览  ━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  原始搜索结果    {total_raw:>5} 条")
    print(f"    ├ 已抓取       {scraped:>5} 条")
    print(f"    ├ 待抓取       {pending:>5} 条")
    print(f"    ├ 抓取失败     {failed:>5} 条")
    print(f"    └ 需人工补录   {manual:>5} 条")
    print()
    print(f"  AI 分析结果     {analyzed:>5} 条")
    print(f"    ├ 🔴 高影响    {high_impact:>5} 条  ({high_impact/analyzed*100:.0f}%)" if analyzed else
          f"    ├ 🔴 高影响        0 条")
    print(f"    ├ 🟡 中影响    {mid_impact:>5} 条")
    print(f"    └ 🟢 低影响    {low_impact:>5} 条")
    print()


def cmd_reset(args: list[str]):
    import os
    import glob
    from config import DATABASE_PATH, REPORTS_DIR
    from database import init_db

    do_db      = "--db"      in args or not args
    do_reports = "--reports" in args or not args

    targets = []
    if do_db:      targets.append(f"数据库（{DATABASE_PATH}）")
    if do_reports: targets.append(f"报告文件（{REPORTS_DIR}/*.xlsx）")

    print("\n即将删除：")
    for t in targets:
        print(f"  • {t}")
    ans = input("\n确认删除？[y/N] ").strip().lower()
    if ans != "y":
        print("已取消。")
        return

    if do_db:
        for path in (DATABASE_PATH, DATABASE_PATH + "-wal", DATABASE_PATH + "-shm"):
            if os.path.exists(path):
                os.remove(path)
                print(f"  ✓ 已删除 {path}")
        init_db()
        print("  ✓ 数据库已重新初始化")

    if do_reports:
        xlsx_files = glob.glob(f"{REPORTS_DIR}/*.xlsx")
        for f in xlsx_files:
            os.remove(f)
        print(f"  ✓ 已删除 {len(xlsx_files)} 个报告文件")

    print("\n重置完成。")


def cmd_init(_args: list[str]):
    from database import init_db
    init_db()
    print("数据库初始化完成。")


def cmd_seed(_args: list[str]):
    from seeds import run_seed_command
    run_seed_command()


def cmd_evaluate(_args: list[str]):
    from evaluate import run_evaluate_command
    run_evaluate_command()


# ── 交互菜单 ──────────────────────────────────────────────────────────────────

# (command | None=separator, display description)
_MENU: list[tuple[str, str] | None] = [
    ("run",      "完整流水线：Gemini情报研究 → 生成报告  附加：--quick（仅新发布）"),
    ("research", "Gemini 情报研究（搜索+合成）  附加：--quick"),
    ("scrape",   "抓取所有待处理 URL 的网页正文"),
    ("analyze",  "对已抓取内容进行 AI 合规分析"),
    ("report",   "生成 Excel 周报（保存至 reports/）"),
    None,
    ("view",     "终端查看分析结果  附加参数：高/中/低  [条数，默认20]"),
    ("status",   "数据库统计概览"),
    ("backfill", "补全缺失的来源 URL（Gemini 逐条查找官方链接）"),
    ("manual",   "人工补录：粘贴抓取失败页面正文"),
    ("retry",    "重试失败/需人工 URL（重置为待抓取）"),
    ("clean",    "清除「不相关」分析记录（仅删除，不重新分析）"),
    ("reanalyze","重置「不相关」条目并立即重新分析"),
    ("reset",    "清空数据  附加参数：--db  --reports（默认全部）"),
    ("init",     "初始化数据库（首次使用时运行）"),
    ("seed",     "注入种子源(已知权威源 URL 列表，保底召回)"),
    ("evaluate", "对照黄金集计算召回率(tests/gold_set.json)"),
]

_NUMBERED = [e for e in _MENU if e is not None]  # for index lookup


def _print_menu():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              GRIS — 全球法规情报系统                         ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print(f"  {'编号':<5}  {'命令':<10}  说明")
    print(f"  {'─'*4}  {'─'*10}  {'─'*42}")
    idx = 1
    for entry in _MENU:
        if entry is None:
            print()
            continue
        cmd, desc = entry
        print(f"  [{idx:>2}]   {cmd:<10}  {desc}")
        idx += 1
    print()
    print("  输入命令名或编号执行，支持附加参数（如 search --quick）")
    print("  输入 q 或直接回车退出")
    print()


def cmd_menu(_args: list[str]):
    while True:
        _print_menu()
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw or raw.lower() in ("q", "exit", "quit"):
            break

        parts    = raw.split()
        first    = parts[0]
        cmd_args = parts[1:]

        if first.isdigit():
            n = int(first)
            if 1 <= n <= len(_NUMBERED):
                cmd_name = _NUMBERED[n - 1][0]
            else:
                print(f"\n  编号超出范围（1–{len(_NUMBERED)}），请重新输入。")
                continue
        else:
            cmd_name = first

        if cmd_name not in COMMANDS:
            print(f"\n  未知命令：{cmd_name!r}，请重新输入。")
            continue

        print()
        COMMANDS[cmd_name](cmd_args)

        try:
            input("\n  按回车返回菜单...")
        except (EOFError, KeyboardInterrupt):
            print()
            break


# ── 路由 ──────────────────────────────────────────────────────────────────────

COMMANDS = {
    "run":       cmd_run,
    "research":  cmd_research,
    "backfill":  cmd_backfill,
    "scrape":    cmd_scrape,
    "analyze":   cmd_analyze,
    "report":    cmd_report,
    "view":      cmd_view,
    "status":    cmd_status,
    "manual":    cmd_manual,
    "retry":     cmd_retry,
    "clean":     cmd_clean,
    "reanalyze": cmd_reanalyze,
    "reset":     cmd_reset,
    "init":      cmd_init,
    "seed":      cmd_seed,
    "evaluate":  cmd_evaluate,
    "menu":      cmd_menu,
}


def main():
    args = sys.argv[1:]

    if not args:
        cmd_menu([])
        return

    if args[0] in ("-h", "--help", "help"):
        print(HELP)
        return

    cmd = args[0]
    if cmd not in COMMANDS:
        print(f"未知命令：{cmd!r}")
        print("运行 python gris.py 进入交互菜单，或 python gris.py --help 查看所有命令。")
        sys.exit(1)

    COMMANDS[cmd](args[1:])


if __name__ == "__main__":
    main()
