import os as _os
_BASE = _os.path.dirname(_os.path.abspath(__file__))

# API Keys — 实际密钥写在 config_local.py(已在 .gitignore 中,不会上传)
try:
    from config_local import GEMINI_API_KEY
except ImportError:
    GEMINI_API_KEY = _os.environ.get("GEMINI_API_KEY", "")

# Database
DATABASE_PATH = _os.path.join(_BASE, "data", "gris.db")

# Paths
REPORTS_DIR = _os.path.join(_BASE, "reports")
LOGS_DIR    = _os.path.join(_BASE, "logs")

# ── Product lines ─────────────────────────────────────────────────────────────
# Single source of truth. When adding a new product, append it here.
PRODUCT_LINES = [
    "电动滑板车",
    "电动平衡车",
    "电助力自行车",   # 原 "Ebike"，含共享场景
    "电动摩托车",     # 原 "电动轻型摩托车"
    "智能割草机",
]
# 注：共享电动滑板车已并入"电动滑板车"，不再单独列出

# 关键零部件（独立搜索槽，合规义务最终归因到整机产品线）
COMPONENT_LINES = [
    "锂电池组",
    "电机驱动系统",
    "控制器",
    "充电器",
    "BMS电池管理系统",
]

VALID_PRODUCTS = set(PRODUCT_LINES)
