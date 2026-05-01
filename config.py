import os as _os
_BASE = _os.path.dirname(_os.path.abspath(__file__))

# API Keys — 实际密钥写在 config_local.py(已在 .gitignore 中,不会上传)
try:
    from config_local import GEMINI_API_KEY
except ImportError:
    GEMINI_API_KEY = _os.environ.get("GEMINI_API_KEY", "")


def require_api_key() -> str:
    """读取 API key,空时给清晰提示。LLM 调用前应调一次。
    比让 SDK 报 401 更早暴露配置问题——避免用户跑半小时后才发现 key 没配。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY 未配置。请在以下任一位置设置:\n"
            "  1. 项目根目录创建 config_local.py,内容:GEMINI_API_KEY = \"AIza...\"\n"
            "  2. 或设置环境变量:export GEMINI_API_KEY=\"AIza...\"\n"
            "申请地址:https://aistudio.google.com/apikey"
        )
    return GEMINI_API_KEY

# Database
DATABASE_PATH = _os.path.join(_BASE, "data", "gris.db")

# Paths
REPORTS_DIR = _os.path.join(_BASE, "reports")
LOGS_DIR    = _os.path.join(_BASE, "logs")

# ── Product lines ─────────────────────────────────────────────────────────────
# Single source of truth. When adding a new product, append it here.
# VALID_PRODUCTS 在 classify.py 派生（避免重复定义）。
PRODUCT_LINES = [
    "电动滑板车",
    "电动平衡车",
    "电助力自行车",   # 原 "Ebike"，含共享场景
    "电动摩托车",     # 原 "电动轻型摩托车"
    "智能割草机",
]
# 注：共享电动滑板车已并入"电动滑板车"，不再单独列出


# ── 去重 / 替换窗口(天) ─────────────────────────────────────────────────────────
# 用途两处:
#   1. researcher 入库去重:同一 title hash 在窗口内不重复入 raw_search_results
#   2. analyzer.main 替换合成版:窗口内同 hash 旧合成版若被新真原文覆盖会触发 DELETE+INSERT
#
# 选 90 天的考量:用户业务节奏是每月跑 2-3 次(约每 10-15 天一次),
# 一条 ⚠️ 合成版应该在多次跑之间有充足机会被真原文替换。
# 30 天太短(每条最多 2-3 次替换机会就过期);90 天给 6-8 次机会,
# 跟"10-12 个月缓冲期"的业务节奏对齐。
DEDUP_WINDOW_DAYS = 90
