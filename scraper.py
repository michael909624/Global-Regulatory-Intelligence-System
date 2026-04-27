"""
Scraper：从官方 URL 抓取法规原文。

职责：纯 HTTP 抓取 + 文本提取，不做任何内容分析。
- HTML：requests + BeautifulSoup
- PDF：pdfplumber

特性：
- per-domain 速率控制（同一域两次请求间至少 1.5s）
- 文本超过 _MAX_CHARS 时标记 truncated=1
"""
from __future__ import annotations

import io
import json
import random
import re
import threading
import time
from collections import defaultdict
from urllib.parse import quote_plus, unquote, urlparse

import pdfplumber
import requests
from bs4 import BeautifulSoup

from database import (
    init_db,
    get_pending_scrape,
    insert_scraped_content,
    update_scrape_status,
)
from utils import get_logger

_log = get_logger("scraper")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,ja;q=0.6,ko;q=0.5,ru;q=0.4",
}
_TIMEOUT       = 25       # 单次请求超时（秒）
_MAX_CHARS     = 60_000   # 单条记录最长保留字符数
_MAX_PDF_PAGES = 80
_PDF_PAGE_LIMIT_REACHED = "PDF_PAGE_LIMIT"

_NOISE_TAGS = ["script", "style", "nav", "header", "footer",
               "aside", "noscript", "form", "iframe"]

# per-domain 速率控制
_PER_DOMAIN_GAP = 1.5     # 同域请求间隔下限（秒）
_domain_last_hit: dict[str, float] = defaultdict(float)
_domain_lock = threading.Lock()


# ── EUR-Lex → Cellar 重写 ─────────────────────────────────────────────────────
# eur-lex.europa.eu 由 CloudFront + AWS WAF 保护，requests 直接拿到的是
# HTTP 202 + 空 body（x-amzn-waf-action: challenge）。改走 publications.europa.eu
# 的 Cellar 内容协商接口可绕过：
#   GET https://publications.europa.eu/resource/celex/{CELEX}
#   Accept: application/pdf      Accept-Language: eng
# 跟随 303 重定向后返回真正的 PDF 字节流。

_ELI_TYPE_TO_CELEX = {
    "reg": "R",      # Regulation
    "dir": "L",      # Directive
    "dec": "D",      # Decision
    "reg_impl": "R",
    "reg_del":  "R",
    "dir_impl": "L",
    "dir_del":  "L",
}

_CELEX_RE = re.compile(r"CELEX[:\s]*(\d{5}[A-Z]\d{4})", re.IGNORECASE)
_ELI_RE = re.compile(
    r"/eli/(reg_impl|reg_del|dir_impl|dir_del|reg|dir|dec)/(\d{4})/(\d+)",
    re.IGNORECASE,
)


def _extract_celex(url: str) -> str | None:
    """从 EUR-Lex URL 提取 CELEX 号（如 32023R1542）。"""
    if not url:
        return None
    decoded = unquote(url)

    m = _CELEX_RE.search(decoded)
    if m:
        return m.group(1).upper()

    m = _ELI_RE.search(url)
    if m:
        eli_type = m.group(1).lower()
        year     = m.group(2)
        num      = m.group(3)
        letter   = _ELI_TYPE_TO_CELEX.get(eli_type)
        if letter:
            return f"3{year}{letter}{int(num):04d}"

    return None


def _rewrite_eur_lex(url: str) -> str | None:
    """若 URL 指向 eur-lex.europa.eu，返回对应的 Cellar 直链；否则 None。"""
    host = urlparse(url).netloc.lower()
    if "eur-lex.europa.eu" not in host:
        return None
    celex = _extract_celex(url)
    if not celex:
        return None
    return f"https://publications.europa.eu/resource/celex/{celex}"


# ── gov.uk URL 智能恢复 ───────────────────────────────────────────────────────
# Gemini 经常给出 gov.uk 上接近但不存在的 slug（多/少 "the-" 前缀等）。
# 用 gov.uk 官方 search API 按 title 模糊匹配回正确路径。

_GOV_UK_STOPWORDS = {
    "the", "of", "and", "or", "for", "to", "a", "an", "on", "in",
    "act", "regulations", "regulation", "order", "rules", "rule",
    "directive", "bill", "statutory", "instrument", "no",
}


def _recover_gov_uk(title: str) -> str | None:
    """用 gov.uk search API 按 title 找正确路径；通过 token 重叠校验避免拉回不相关结果。"""
    if not title:
        return None

    cleaned = re.sub(r"\([^)]*\)", " ", title)              # 去括号内容
    tokens  = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", cleaned)
    keep    = [t for t in tokens if t.lower() not in _GOV_UK_STOPWORDS and len(t) > 2]
    if not keep:
        return None
    query = " ".join(keep[:6])                              # 取前 6 个关键词
    title_words = {t.lower() for t in keep}

    try:
        resp = requests.get(
            f"https://www.gov.uk/api/search.json?q={quote_plus(query)}&count=5",
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        results = (resp.json() or {}).get("results", []) or []
    except Exception as e:
        _log.warning("gov.uk recovery failed for %r: %s", title[:40], e)
        return None

    best_url: str | None = None
    best_overlap = 0
    for r in results:
        cand_title = (r.get("title") or "").lower()
        cand_words = set(re.findall(r"[a-z][a-z0-9\-]+", cand_title))
        overlap    = len(title_words & cand_words)
        if overlap > best_overlap:
            link = (r.get("link") or "").strip()
            if link.startswith("/"):
                best_url, best_overlap = "https://www.gov.uk" + link, overlap
            elif link.startswith("http"):
                best_url, best_overlap = link, overlap

    # 至少 3 个关键词重叠才算有效匹配，否则放弃
    return best_url if best_overlap >= 3 else None


def _wait_for_domain(host: str) -> None:
    with _domain_lock:
        last = _domain_last_hit[host]
        wait = _PER_DOMAIN_GAP - (time.time() - last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.3))
        _domain_last_hit[host] = time.time()


def _extract_pdf(content: bytes) -> tuple[str | None, bool]:
    """返回 (text, truncated)。truncated=True 表示页数超过 _MAX_PDF_PAGES。"""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            n_pages = len(pdf.pages)
            pages = [p.extract_text() or "" for p in pdf.pages[:_MAX_PDF_PAGES]]
        text = "\n".join(pages).strip()
        return (text or None, n_pages > _MAX_PDF_PAGES)
    except Exception as e:
        _log.warning("PDF parse failed: %s", e)
        return (None, False)


def _extract_html(content: bytes) -> str | None:
    try:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(_NOISE_TAGS):
            tag.decompose()
        for selector in ("main", "article"):
            el = soup.find(selector)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text
        text = soup.get_text(separator="\n", strip=True)
        return text.strip() or None
    except Exception as e:
        _log.warning("HTML parse failed: %s", e)
        return None


def scrape_url(url: str) -> tuple[str | None, str, bool]:
    """
    抓取并提取文本。
    返回 (text, content_type, truncated)。失败时 text=None。
    """
    if not url or not url.startswith("http"):
        return None, "unknown", False

    cellar = _rewrite_eur_lex(url)
    if cellar:
        url = cellar
        headers = {**_HEADERS, "Accept": "application/pdf", "Accept-Language": "eng"}
        force_pdf = True
    else:
        headers = _HEADERS
        force_pdf = False

    host = urlparse(url).netloc.lower()
    _wait_for_domain(host)

    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True)

        waf_action = resp.headers.get("x-amzn-waf-action", "").lower()
        if waf_action == "challenge" or (resp.status_code == 202 and not resp.content):
            _log.warning("WAF/empty challenge on %s (status=%s, waf=%s)",
                         url, resp.status_code, waf_action or "—")
            return None, "unknown", False

        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "").lower()
        is_pdf = force_pdf or "pdf" in ct or url.lower().split("?")[0].endswith(".pdf")

        if not resp.content or len(resp.content) < 200:
            _log.warning("empty body on %s (size=%d)", url, len(resp.content))
            return None, "unknown", False

        if is_pdf:
            text, page_truncated = _extract_pdf(resp.content)
            ctype = "pdf"
        else:
            text = _extract_html(resp.content)
            page_truncated = False
            ctype = "webpage"

        if text and len(text) > _MAX_CHARS:
            return text[:_MAX_CHARS], ctype, True
        return text, ctype, page_truncated

    except requests.RequestException as e:
        _log.warning("HTTP error %s: %s", url, e)
        return None, "unknown", False
    except Exception as e:
        _log.warning("scrape error %s: %s", url, e)
        return None, "unknown", False


def _row_get(row, key: str) -> str:
    """安全读 sqlite3.Row 字段（缺列时返回空串）。"""
    try:
        v = row[key]
    except (IndexError, KeyError):
        return ""
    return (v or "").strip() if isinstance(v, str) else (v or "")


def _try_scrape_chain(row) -> tuple[str | None, str, bool, str | None]:
    """
    依序尝试：主 URL → fallback URLs → 智能恢复（gov.uk search API）。
    返回 (text, ctype, truncated, used_url)。全部失败 → text=None。
    """
    main_url = _row_get(row, "source_url")
    title    = _row_get(row, "title")

    candidates: list[str] = []
    if main_url:
        candidates.append(main_url)

    fb_raw = _row_get(row, "fallback_urls")
    if fb_raw:
        try:
            fb = json.loads(fb_raw)
            if isinstance(fb, list):
                for u in fb:
                    if isinstance(u, str) and u.startswith("http") and u not in candidates:
                        candidates.append(u)
        except Exception:
            pass

    # gov.uk 主 URL 失败时的智能恢复
    main_host = urlparse(main_url).netloc.lower() if main_url else ""
    if "gov.uk" in main_host:
        rec = _recover_gov_uk(title)
        if rec and rec not in candidates:
            candidates.append(rec)

    for url in candidates:
        text, ctype, truncated = scrape_url(url)
        if text:
            return text, ctype, truncated, url

    return None, "unknown", False, None


def scrape_all() -> tuple[int, int, int]:
    """
    抓取所有 scrape_status='待抓取' 的记录。
    返回 (succeeded, failed, manual)。
    """
    init_db()
    pending = get_pending_scrape()
    if not pending:
        print("  没有待抓取的记录。")
        return 0, 0, 0

    total     = len(pending)
    succeeded = failed = manual = 0

    print(f"\n  共 {total} 条待抓取\n")

    for i, row in enumerate(pending, 1):
        url   = _row_get(row, "source_url")
        title = (_row_get(row, "title") or "")[:50]
        print(f"  [{i:>3}/{total}] {title}", end=" ... ", flush=True)

        if not url and not _row_get(row, "fallback_urls"):
            update_scrape_status(row["id"], "需人工")
            manual += 1
            print("无 URL → 需人工")
            continue

        text, ctype, truncated, used_url = _try_scrape_chain(row)
        try:
            if text:
                insert_scraped_content({
                    "raw_id":       row["id"],
                    "full_text":    text,
                    "content_type": ctype,
                    "truncated":    1 if truncated else 0,
                })
                # 若成功 URL 与主 URL 不同，回写为新的 source_url（便于后续追溯）
                if used_url and used_url != url:
                    from database import get_connection
                    with get_connection() as conn:
                        conn.execute(
                            "UPDATE raw_search_results SET source_url=? WHERE id=?",
                            (used_url, row["id"]),
                        )
                update_scrape_status(row["id"], "已抓取")
                succeeded += 1
                trunc_tag    = " ✂截断" if truncated else ""
                fallback_tag = " ↩fallback" if used_url and used_url != url else ""
                print(f"✓ {ctype}  ({len(text):,} chars){trunc_tag}{fallback_tag}")
            else:
                update_scrape_status(row["id"], "失败")
                failed += 1
                print("✗ 抓取失败")
        except Exception as db_err:
            failed += 1
            _log.error("DB write failed raw_id=%d: %s", row["id"], db_err)
            print("✗ 数据库写入失败")

    print(f"\n  抓取完成：成功 {succeeded}，失败 {failed}，无 URL {manual}")
    return succeeded, failed, manual


if __name__ == "__main__":
    scrape_all()
