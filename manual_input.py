"""
人工补录入口
────────────────────────────────────────────────────
运行方式：python3 manual_input.py
       或：python3 gris.py manual

列出所有 scrape_status='需人工' 的记录，逐条让你粘贴正文。
补录时会自动清理旧的 [Gemini synthesis] 分析（避免重复）。
"""
from __future__ import annotations

from database import (
    get_connection,
    insert_scraped_content,
    update_scrape_status,
)


def _get_pending() -> list:
    sql = """
        SELECT id, title, source_url
        FROM raw_search_results
        WHERE scrape_status = '需人工'
        ORDER BY query_date DESC
    """
    with get_connection() as conn:
        return conn.execute(sql).fetchall()


def _purge_synthesized(raw_id: int) -> int:
    """补录前清理同一 raw_id 下的旧 Gemini 合成内容及其分析。"""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id FROM scraped_content
            WHERE raw_id = ? AND full_text LIKE '[Gemini synthesis]%'
        """, (raw_id,)).fetchall()
        if not rows:
            return 0
        sc_ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(sc_ids))
        conn.execute(
            f"DELETE FROM compliance_analysis WHERE scraped_id IN ({ph})", sc_ids
        )
        conn.execute(
            f"DELETE FROM scraped_content WHERE id IN ({ph})", sc_ids
        )
        return len(sc_ids)


def _read_remaining() -> str:
    """读取多行粘贴内容；用户在新行单独输入 END 结束。"""
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def run() -> None:
    records = _get_pending()
    if not records:
        print("没有需要人工补录的记录。")
        return

    total = len(records)
    print(f"\n共 {total} 条记录需要人工补录。")
    print("操作说明：")
    print("  • 用浏览器打开链接，全选复制页面正文")
    print("  • 粘贴到终端后，在新的一行单独输入 END 并按回车")
    print("  • 输入 SKIP 跳过当前条目\n")

    done = skipped = 0

    for i, row in enumerate(records, 1):
        raw_id = row["id"]
        title  = row["title"] or "（无标题）"
        url    = row["source_url"] or "（无链接）"

        print("=" * 62)
        print(f"[{i}/{total}] {title}")
        print(f"链接：{url}")
        print("=" * 62)
        print("请复制页面正文后粘贴到此处，然后单独输入 END 回车：")
        print("（直接输入 SKIP 回车可跳过）")
        print()

        first_line = input().strip()

        if first_line.upper() == "SKIP":
            print("已跳过。\n")
            skipped += 1
            continue

        rest = _read_remaining()
        text = (first_line + ("\n" + rest if rest else "")).strip()

        if not text:
            print("内容为空，已跳过。\n")
            skipped += 1
            continue

        purged = _purge_synthesized(raw_id)
        if purged:
            print(f"  ↺ 已清理旧 Gemini 合成记录 {purged} 条")

        insert_scraped_content({
            "raw_id":       raw_id,
            "full_text":    text,
            "content_type": "webpage",
        })
        update_scrape_status(raw_id, "已抓取")
        done += 1
        print(f"✓ 已保存（{len(text)} 字符）\n")

    print("=" * 62)
    print(f"补录完成：成功 {done} 条，跳过 {skipped} 条。")
    if done:
        print("现在可以运行分析：python3 gris.py analyze")
    print("=" * 62)


if __name__ == "__main__":
    run()
