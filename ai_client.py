"""
统一的 Gemini 客户端封装。

把模型选择、重试策略、temperature 默认值集中在这里,
researcher / analyzer / fetch_sources 都用这两个函数,无需各自维护。

两条主路径:
  • call_grounded — 带 Google Search 工具,用于"发现"或"内容补全"
  • call_json     — 关闭 grounding,响应必为 JSON,用于"分析"

未来要换模型、加缓存、加成本统计、改重试 — 只改这一个文件。
"""
from __future__ import annotations

import threading
import time

from google import genai
from google.genai import types

from config import GEMINI_API_KEY
from utils import get_logger

_log = get_logger("ai_client")

# ── 默认配置(集中管理,方便统一调整) ─────────────────────────────────────────
DEFAULT_MODEL    = "gemini-flash-latest"
DEFAULT_RETRIES  = 2     # 总尝试次数 = retries + 1

# Temperature 分层默认:
#   发现层(grounded)  → 1.0  最大化召回,接受变化
#   分析层(JSON)      → 0.0  保证同一输入稳定输出
DEFAULT_TEMP_GROUNDED = 1.0
DEFAULT_TEMP_JSON     = 0.0


# ── 客户端单例 ────────────────────────────────────────────────────────────────

_client: genai.Client | None = None
_client_lock = threading.Lock()


def get_client() -> genai.Client:
    """单例 Gemini client。线程安全。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ── 重试封装 ──────────────────────────────────────────────────────────────────

def _is_rate_limit(err: Exception) -> bool:
    s = str(err).lower()
    return "429" in s or "quota" in s or "rate" in s


def _do_call_with_retry(
    *,
    label: str,
    model: str,
    prompt: str,
    cfg: types.GenerateContentConfig,
    retries: int,
):
    """共享重试逻辑;返回 SDK 原始 response。"""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return get_client().models.generate_content(
                model=model, contents=prompt, config=cfg,
            )
        except Exception as e:
            last_err = e
            if attempt < retries:
                # 限流时等更久,常规错误指数退避
                wait = 60 if _is_rate_limit(e) else 8 * (attempt + 1)
                _log.warning(
                    "%s attempt %d/%d failed: %s — wait %ds",
                    label, attempt + 1, retries + 1, e, wait,
                )
                time.sleep(wait)
    raise RuntimeError(f"{label} failed after {retries + 1} attempts: {last_err}")


# ── 公共 API ──────────────────────────────────────────────────────────────────

def call_grounded(
    prompt: str,
    *,
    system: str,
    temperature: float = DEFAULT_TEMP_GROUNDED,
    top_p: float | None = 0.95,
    model: str = DEFAULT_MODEL,
    retries: int = DEFAULT_RETRIES,
    return_sources: bool = True,
) -> tuple[str, list[dict]]:
    """
    带 Google Search grounding 的调用。
    返回 (text, sources)。sources 为空列表表示无 grounding 数据。

    return_sources=False 时跳过 sources 解析(节省一点点),仍返回元组以兼容签名。
    """
    cfg_kwargs = {
        "tools": [types.Tool(google_search=types.GoogleSearch())],
        "system_instruction": system,
        "temperature": temperature,
    }
    if top_p is not None:
        cfg_kwargs["top_p"] = top_p
    cfg = types.GenerateContentConfig(**cfg_kwargs)

    resp = _do_call_with_retry(
        label="call_grounded", model=model, prompt=prompt, cfg=cfg, retries=retries,
    )

    text = resp.text or ""
    sources: list[dict] = []
    if return_sources:
        try:
            meta   = resp.candidates[0].grounding_metadata
            chunks = (meta.grounding_chunks or []) if meta else []
            for c in chunks:
                if c.web:
                    sources.append({"url": c.web.uri or "", "title": c.web.title or ""})
        except (IndexError, AttributeError):
            pass
    return text, sources


def call_json(
    prompt: str,
    *,
    system: str,
    temperature: float = DEFAULT_TEMP_JSON,
    model: str = DEFAULT_MODEL,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """
    无 grounding,要求 JSON 响应。
    返回原始 text(调用方负责 parse)。
    """
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=temperature,
    )
    resp = _do_call_with_retry(
        label="call_json", model=model, prompt=prompt, cfg=cfg, retries=retries,
    )
    return resp.text or ""
