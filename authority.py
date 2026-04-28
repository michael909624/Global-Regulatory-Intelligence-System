"""
域名权威度评分（Stage 4）。

用途：
  1. Stage 0 同 reg_id 多候选时选主 URL
  2. 报告里 hyperlink 指向最权威官方源（不是模型给的随便一个）
  3. Stage 3 LLM consolidation 选 keep_id 时的 tie-breaker

评分梯度：
  100  法规正本 / 官方公报（eur-lex / federalregister / legislation.gov.uk / GPO）
   85-95  政府部委 / 国家监管机构（ec.europa.eu / gov.uk / cpsc.gov / meti.go.jp）
   70-80  标准机构（CENELEC / IEC / ISO / DIN / NIST）
   40-60  协会 / 检测 / 行业（UL / NFPA / IATA）
   10  其他默认
"""
from __future__ import annotations

from urllib.parse import urlparse


_AUTHORITY_BY_HOST: dict[str, int] = {
    # ── tier 1：法规正本 / 官方公报 ───────────────────────────────────────────
    "eur-lex.europa.eu":         100,
    "publications.europa.eu":    100,
    "federalregister.gov":       100,
    "legislation.gov.uk":        100,
    "gpo.gov":                   100,
    "openstd.samr.gov.cn":        95,
    "std.samr.gov.cn":            95,
    "elaws.e-gov.go.jp":          95,
    "law.go.kr":                  95,
    "elaw.klri.re.kr":            95,
    "regulations.gov":            95,

    # ── tier 2：政府部委 / 监管机构 ───────────────────────────────────────────
    "ec.europa.eu":               90,
    "europa.eu":                  85,
    "gov.uk":                     90,
    "samr.gov.cn":                90,
    "cnca.gov.cn":                90,
    "miit.gov.cn":                90,
    "cpsc.gov":                   90,
    "nhtsa.dot.gov":              90,
    "dot.gov":                    85,
    "fcc.gov":                    85,
    "ftc.gov":                    85,
    "epa.gov":                    85,
    "osha.gov":                   80,
    "meti.go.jp":                 90,
    "mlit.go.jp":                 90,
    "nite.go.jp":                 85,
    "caa.go.jp":                  85,
    "soumu.go.jp":                85,
    "motie.go.kr":                90,
    "molit.go.kr":                90,
    "kats.go.kr":                 90,
    "kcc.go.kr":                  85,
    "mois.go.kr":                 85,
    "accc.gov.au":                90,
    "productsafety.gov.au":       90,
    "energy.gov.au":              80,
    "infrastructure.gov.au":      85,
    "canada.ca":                  90,
    "tc.gc.ca":                   90,
    "hc-sc.gc.ca":                90,
    "recalls-rappels.canada.ca":  90,
    "cpsa.ca":                    80,
    "unece.org":                  85,

    # ── tier 3：标准机构 ─────────────────────────────────────────────────────
    "nist.gov":                   85,
    "cenelec.eu":                 75,
    "cen.eu":                     75,
    "iec.ch":                     75,
    "iso.org":                    75,
    "etsi.org":                   75,
    "din.de":                     75,
    "afnor.org":                  75,
    "uni.com":                    70,
    "bsi.org.uk":                 75,
    "standards.org.au":           75,
    "ansi.org":                   70,
    "kssn.net":                   65,

    # ── tier 4：协会 / 检测 / 行业 ────────────────────────────────────────────
    "ul.com":                     55,
    "nfpa.org":                   55,
    "iata.org":                   60,
    "imo.org":                    60,
    "icao.int":                   60,
}

# 后缀兜底：top-level 政府域名给中等权威分
_SUFFIX_FALLBACK: list[tuple[str, int]] = [
    (".gov.uk",     85),
    (".gov.cn",     85),
    (".gov.au",     85),
    (".gov.in",     80),
    (".go.jp",      80),
    (".go.kr",      80),
    (".europa.eu",  75),
    (".gc.ca",      80),
    (".gov",        75),
    (".int",        50),
]


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def score(url: str) -> int:
    """URL → 权威度（0–100）。未知域 = 10。"""
    host = _host_of(url)
    if not host:
        return 0

    if host in _AUTHORITY_BY_HOST:
        return _AUTHORITY_BY_HOST[host]

    # 子域匹配（如 eb.gov.uk 走 .gov.uk 后缀兜底；某些子域明确更权威则在表里 override）
    for known, sc in _AUTHORITY_BY_HOST.items():
        if host.endswith("." + known):
            return sc

    for suffix, sc in _SUFFIX_FALLBACK:
        if host.endswith(suffix):
            return sc

    return 10


def best_url(urls: list[str]) -> str:
    """从候选 URL 列表选最权威的。同分按 URL 长度（短优先，通常更稳定）。"""
    if not urls:
        return ""
    return max(urls, key=lambda u: (score(u), -len(u)))


def sort_by_authority(urls: list[str]) -> list[str]:
    """按权威度降序去重排序候选 URL。"""
    seen: set[str] = set()
    uniq: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    uniq.sort(key=lambda u: (-score(u), len(u)))
    return uniq
