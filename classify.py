"""
产品分类、市场分级、来源机构识别。
所有函数为纯函数，无外部 I/O，可独立单测。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from config import PRODUCT_LINES

VALID_PRODUCTS: set[str] = set(PRODUCT_LINES)


# ── 产品别名映射（多语言 → 标准 5 类整机）────────────────────────────────────

_PRODUCT_ALIAS: dict[str, str] = {
    # 电助力自行车
    "Ebike":                   "电助力自行车",
    "E-bike":                  "电助力自行车",
    "ebike":                   "电助力自行车",
    "e-bike":                  "电助力自行车",
    "electric bicycle":        "电助力自行车",
    "electric bike":           "电助力自行车",
    "pedelec":                 "电助力自行车",
    "Pedelec":                 "电助力自行车",
    "EPAC":                    "电助力自行车",
    "epac":                    "电助力自行车",
    "S-Pedelec":               "电助力自行车",
    "speed pedelec":           "电助力自行车",
    "PAB":                     "电助力自行车",
    "power-assisted bicycle":  "电助力自行车",
    "L1e-A":                   "电助力自行车",
    "電動アシスト自転車":        "电助力自行车",
    "전기자전거":                "电助力自行车",
    "электровелосипед":         "电助力自行车",
    "电助力":                   "电助力自行车",
    "助力自行车":               "电助力自行车",
    "电动自行车":               "电助力自行车",
    # 电动摩托车
    "电摩":                     "电动摩托车",
    "电动轻型摩托车":           "电动摩托车",
    "轻型摩托车":               "电动摩托车",
    "电动摩托":                 "电动摩托车",
    "L1e-B":                   "电动摩托车",
    "L3e":                     "电动摩托车",
    "electric motorcycle":     "电动摩托车",
    "electric moped":          "电动摩托车",
    "e-moped":                 "电动摩托车",
    "電動バイク":                "电动摩托车",
    "전기 오토바이":             "电动摩托车",
    # 电动滑板车（吸收旧共享类名称）
    "共享电动滑板车":           "电动滑板车",
    "共享滑板车":               "电动滑板车",
    "踏板车":                   "电动滑板车",
    "电动踏板车":               "电动滑板车",
    "电动踢板车":               "电动滑板车",
    "electric scooter":        "电动滑板车",
    "e-scooter":               "电动滑板车",
    "kick scooter":            "电动滑板车",
    "e-kick scooter":          "电动滑板车",
    "micromobility scooter":   "电动滑板车",
    "standing e-scooter":      "电动滑板车",
    "電動キックボード":          "电动滑板车",
    "전동 킥보드":               "电动滑板车",
    "электросамокат":          "电动滑板车",
    # 电动平衡车
    "hoverboard":              "电动平衡车",
    "self-balancing scooter":  "电动平衡车",
    "balance board":           "电动平衡车",
    "self-balancing personal transporter": "电动平衡车",
    "전동 호버보드":             "电动平衡车",
    # 智能割草机
    "robotic lawn mower":      "智能割草机",
    "robot mower":             "智能割草机",
    "robot lawn mower":        "智能割草机",
    "autonomous lawn mower":   "智能割草机",
    "robot grass cutter":      "智能割草机",
    "割草机":                   "智能割草机",
    "机器人割草机":             "智能割草机",
    "自动割草机":               "智能割草机",
    "Mähroboter":              "智能割草机",
    "ロボット草刈機":            "智能割草机",
    "로봇 잔디깎기":             "智能割草机",
}

_ALIAS_LOWER = {alias.lower(): canonical for alias, canonical in _PRODUCT_ALIAS.items()}


def normalize_products(raw: str) -> str:
    """规范化 affected_products 字符串到「、」分隔的标准整机名。"""
    if not raw:
        return "不相关"
    parts = [p.strip() for p in re.split(r"[、,，/]", raw) if p.strip()]
    matched: list[str] = []
    for p in parts:
        if p in VALID_PRODUCTS:
            matched.append(p)
            continue
        if p in _PRODUCT_ALIAS:
            matched.append(_PRODUCT_ALIAS[p])
            continue
        canonical = _ALIAS_LOWER.get(p.lower())
        if canonical:
            matched.append(canonical)
    return "、".join(dict.fromkeys(matched)) if matched else "不相关"


# ── 显示名映射（短交通合并 + 简称）────────────────────────────────────────────

_SHORT_TRANSPORT = {"电动滑板车", "电动平衡车"}
_PRODUCT_DISPLAY_MAP = {
    "电助力自行车": "ebike",
    "电动摩托车":   "电摩",
    "智能割草机":   "割草机",
}


def compute_products_display(products_str: str) -> str:
    if not products_str or products_str == "不相关":
        return products_str or ""
    parts = [p.strip() for p in products_str.split("、") if p.strip()]
    has_short = any(p in _SHORT_TRANSPORT for p in parts)
    others = [_PRODUCT_DISPLAY_MAP.get(p, p) for p in parts if p not in _SHORT_TRANSPORT]
    return "、".join((["短交通"] if has_short else []) + others)


# ── 市场分级 ──────────────────────────────────────────────────────────────────

_EU_SINGLE_CTRY = {
    "德国", "法国", "意大利", "西班牙", "荷兰", "比利时", "瑞典", "丹麦",
    "芬兰", "挪威", "波兰", "奥地利", "瑞士", "葡萄牙", "捷克", "匈牙利",
    "罗马尼亚", "希腊", "爱尔兰", "卢森堡", "斯洛伐克", "斯洛文尼亚",
    "克罗地亚", "保加利亚", "爱沙尼亚", "拉脱维亚", "立陶宛", "塞浦路斯", "马耳他",
}
_US_STATE_KW = {
    "加利福尼亚", "加州", "California",
    "纽约州", "纽约", "New York",
    "德克萨斯", "Texas",
    "华盛顿州", "Washington",
    "科罗拉多", "Colorado",
    "佛罗里达", "Florida",
    "伊利诺伊", "Illinois",
    "马萨诸塞", "Massachusetts",
}
_CA_PROV_KW = {
    "不列颠哥伦比亚", "安大略", "魁北克", "艾伯塔", "卑诗省",
}


def compute_market_tier(markets_str: str) -> int:
    """显示排序档位：0 全球 / 1 欧盟 / 2 单一欧国 / 3 北美 / 4 美联邦 …"""
    s = markets_str or ""

    def _w(kw: str) -> bool:
        return bool(re.search(r"(?<![A-Za-z])" + re.escape(kw) + r"(?![A-Za-z])", s))

    if any(kw in s for kw in ("全球通用", "全球", "Global", "Worldwide", "International")):
        return 0
    if (
        "欧盟" in s or "欧洲全境" in s or "全欧洲" in s
        or "欧洲经济区" in s or "EEA" in s or _w("EU")
    ):
        return 1
    if any(c in s for c in _EU_SINGLE_CTRY) or "英国" in s or _w("UK") or _w("GB"):
        return 2
    if "北美" in s or "North America" in s:
        return 3
    has_us_state = any(st in s for st in _US_STATE_KW)
    has_us_federal = ("联邦" in s and ("美国" in s or _w("US"))) or "美联邦" in s
    if has_us_state and has_us_federal:
        return 4
    if has_us_state:
        return 5
    if has_us_federal or "美国" in s or _w("US"):
        return 4
    if any(p in s for p in _CA_PROV_KW):
        return 7
    if "加拿大" in s or "Canada" in s:
        return 6
    if "澳大利亚" in s or "澳洲" in s or "新西兰" in s or "澳新" in s or _w("AU") or _w("NZ"):
        return 8
    return 99


# ── 来源机构识别 ──────────────────────────────────────────────────────────────

_DOMAIN_MAP = {
    "eur-lex.europa.eu":   ("EUR-Lex",         "EN / 多语言"),
    "ec.europa.eu":        ("欧盟委员会",       "EN"),
    "europa.eu":           ("欧盟",             "EN"),
    "cpsc.gov":            ("美国 CPSC",        "EN"),
    "ftc.gov":             ("美国 FTC",         "EN"),
    "nhtsa.dot.gov":       ("美国 NHTSA",       "EN"),
    "dot.gov":             ("美国 DOT",         "EN"),
    "regulations.gov":     ("美国联邦法规库",    "EN"),
    "federalregister.gov": ("美国联邦公报",      "EN"),
    "cpsa.ca":             ("加拿大 CPSA",      "EN / FR"),
    "canada.ca":           ("加拿大政府",        "EN / FR"),
    "legislation.gov.uk":  ("英国立法网",        "EN"),
    "gov.uk":              ("英国政府",          "EN"),
    "meti.go.jp":          ("日本经产省 METI",   "日文"),
    "mlit.go.jp":          ("日本国交省 MLIT",   "日文"),
    "nite.go.jp":          ("日本 NITE",        "日文"),
    "caa.go.jp":           ("日本消费者厅",      "日文"),
    "motie.go.kr":         ("韩国产业部 MOTIE",  "韩文"),
    "mois.go.kr":          ("韩国行政安全部",    "韩文"),
    "accc.gov.au":         ("澳大利亚 ACCC",    "EN"),
    "energy.gov.au":       ("澳洲能源部",        "EN"),
    "standards.org.au":    ("澳洲标准局",        "EN"),
    "iso.org":             ("ISO",             "EN"),
    "iec.ch":              ("IEC",             "EN"),
    "cenelec.eu":          ("CENELEC",         "EN / 多语言"),
    "etsi.org":            ("ETSI",            "EN"),
    "din.de":              ("DIN 德国标准",      "DE"),
    "bsi.org.uk":          ("BSI 英国标准",      "EN"),
    "ul.com":              ("UL Solutions",    "EN"),
    "ansi.org":            ("ANSI",            "EN"),
    "nfpa.org":            ("NFPA",            "EN"),
    "osha.gov":            ("美国 OSHA",       "EN"),
    "epa.gov":             ("美国 EPA",        "EN"),
    "fcc.gov":             ("美国 FCC",        "EN"),
    "samr.gov.cn":         ("中国市场监管总局 SAMR", "中文"),
    "cnca.gov.cn":         ("中国认监委 CNCA", "中文"),
}
_MARKET_LANG = {
    "JP": "日文", "KR": "韩文", "CN": "中文",
    "DE": "德文", "FR": "法文", "IT": "意大利文",
    "ES": "西班牙文", "RU": "俄文",
}


def institution(url: str, market: str) -> tuple[str, str]:
    """URL → (机构名, 语言)。无法识别时按市场代码兜底。"""
    if not url:
        return ("—", "—")
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        for domain, (name, lang) in _DOMAIN_MAP.items():
            if host == domain or host.endswith("." + domain):
                return (name, lang)
        parts = host.split(".")
        name = parts[-2].upper() if len(parts) >= 2 else host
        lang = _MARKET_LANG.get((market or "").upper(), "EN")
        return (name, lang)
    except Exception:
        return ("—", "—")
