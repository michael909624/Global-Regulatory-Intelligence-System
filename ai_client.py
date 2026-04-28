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
DEFAULT_MODEL    = "gemini-2.5-flash"
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


# ── Token 监控 ────────────────────────────────────────────────────────────────
# 每次 LLM 调用都记录 input/output tokens 到 ai_client.log；
# 累计到 _token_stats 供 cmd_run 末尾汇总打印。

_token_lock  = threading.Lock()
_token_stats: dict = {
    "calls":          0,
    "grounded_calls": 0,
    "input_tokens":   0,
    "output_tokens":  0,
    "by_label":       {},  # label → {calls, in, out}
}


def _record_tokens(label: str, resp, grounded: bool) -> None:
    """从 SDK response 提取 token usage 累计到 _token_stats。"""
    try:
        usage = getattr(resp, "usage_metadata", None)
        if not usage:
            return
        in_tok  = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        with _token_lock:
            _token_stats["calls"]         += 1
            _token_stats["input_tokens"]  += in_tok
            _token_stats["output_tokens"] += out_tok
            if grounded:
                _token_stats["grounded_calls"] += 1
            bl = _token_stats["by_label"].setdefault(
                label, {"calls": 0, "in": 0, "out": 0, "grounded": 0},
            )
            bl["calls"]    += 1
            bl["in"]       += in_tok
            bl["out"]      += out_tok
            bl["grounded"] += 1 if grounded else 0
        _log.info("TOKENS %-15s in=%d out=%d grounded=%s",
                  label, in_tok, out_tok, grounded)
    except Exception as e:
        _log.warning("token tracking failed (%s): %s", label, e)


def get_token_stats() -> dict:
    """返回累计 token 快照（深拷贝）。"""
    with _token_lock:
        return {
            "calls":          _token_stats["calls"],
            "grounded_calls": _token_stats["grounded_calls"],
            "input_tokens":   _token_stats["input_tokens"],
            "output_tokens":  _token_stats["output_tokens"],
            "by_label":       {k: dict(v) for k, v in _token_stats["by_label"].items()},
        }


def reset_token_stats() -> None:
    """重置统计；通常在一次 run 开始时调用。"""
    with _token_lock:
        _token_stats["calls"]          = 0
        _token_stats["grounded_calls"] = 0
        _token_stats["input_tokens"]   = 0
        _token_stats["output_tokens"]  = 0
        _token_stats["by_label"].clear()


# ── 定价常量（Gemini 2.5 Flash, Standard 付费 Tier；以官方文档为准） ────────────
# 参考：https://ai.google.dev/gemini-api/docs/pricing  （核对于 2026-04）
PRICE_INPUT_PER_M    = 0.30   # USD / 1M input tokens (text/image/video)
PRICE_OUTPUT_PER_M   = 2.50   # USD / 1M output tokens (含 thinking tokens)
GROUNDING_FREE_RPD   = 1500   # 每天免费 grounded prompts（与 Flash-Lite 共享）
GROUNDING_PRICE_PER_K = 35.0  # USD / 1k grounded prompts (超出免费额度后)


def print_token_summary(prefix: str = "") -> None:
    """末尾汇总打印——按 label 分组显示 token 消耗 + 估算 USD。"""
    s = get_token_stats()
    if s["calls"] == 0:
        return
    print()
    print((prefix + "━━━ Token & Cost 汇总 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━").rstrip())
    print(f"  总调用：{s['calls']}（其中 grounded {s['grounded_calls']}）")
    print(f"  输入 tokens：{s['input_tokens']:>10,}")
    print(f"  输出 tokens：{s['output_tokens']:>10,}")
    if s["by_label"]:
        print(f"\n  按阶段拆分：")
        for label, bl in sorted(s["by_label"].items(), key=lambda x: -x[1]["in"] - x[1]["out"]):
            grounded_tag = f" 🔍×{bl['grounded']}" if bl["grounded"] else ""
            print(f"    {label:<18} calls={bl['calls']:<4} "
                  f"in={bl['in']:>9,}  out={bl['out']:>7,}{grounded_tag}")
    in_cost  = s["input_tokens"]  / 1_000_000 * PRICE_INPUT_PER_M
    out_cost = s["output_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_M
    paid_grounded = max(0, s["grounded_calls"] - GROUNDING_FREE_RPD)
    g_cost   = paid_grounded / 1000 * GROUNDING_PRICE_PER_K
    print(f"\n  估算成本：${in_cost + out_cost + g_cost:.3f}")
    print(f"    └ input  : ${in_cost:.3f}  ({s['input_tokens']:,} × ${PRICE_INPUT_PER_M}/M)")
    print(f"    └ output : ${out_cost:.3f}  ({s['output_tokens']:,} × ${PRICE_OUTPUT_PER_M}/M)")
    print(f"    └ grounding: ${g_cost:.3f}  ({s['grounded_calls']} 次,免费 {GROUNDING_FREE_RPD}/天后 ${GROUNDING_PRICE_PER_K}/1k)")
    print()


def estimate_cost_usd(stats: dict | None = None) -> float:
    """按 Gemini 2.5 Flash Standard 付费 Tier 估算总成本（美元）。"""
    s = stats or get_token_stats()
    in_cost  = s["input_tokens"]  / 1_000_000 * PRICE_INPUT_PER_M
    out_cost = s["output_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_M
    paid_grounded = max(0, s["grounded_calls"] - GROUNDING_FREE_RPD)
    g_cost   = paid_grounded / 1000 * GROUNDING_PRICE_PER_K
    return in_cost + out_cost + g_cost


# ── 重试封装 ──────────────────────────────────────────────────────────────────

def _is_rate_limit(err: Exception) -> bool:
    """识别限流类错误。用整词/短语匹配，避免 "iterate"/"generate" 误报。"""
    s = str(err).lower()
    return (
        "429" in s
        or "quota" in s
        or "rate limit" in s
        or "rate-limit" in s
        or "ratelimit" in s
        or "too many requests" in s
        or "resource_exhausted" in s
    )


def _do_call_with_retry(
    *,
    label: str,
    model: str,
    prompt: str,
    cfg: types.GenerateContentConfig,
    retries: int,
    grounded: bool = False,
):
    """共享重试逻辑;返回 SDK 原始 response。同时记录 token usage。"""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = get_client().models.generate_content(
                model=model, contents=prompt, config=cfg,
            )
            _record_tokens(label, resp, grounded)
            return resp
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
        grounded=True,
    )

    text = resp.text or ""
    sources: list[dict] = []
    try:
        meta = resp.candidates[0].grounding_metadata
    except (IndexError, AttributeError):
        meta = None

    if meta is not None:
        queries = getattr(meta, "web_search_queries", None) or []
        if queries:
            _log.info("SEARCH_QUERIES (%d): %s", len(queries), " | ".join(queries))
        else:
            _log.info("SEARCH_QUERIES: <none — model answered without searching>")

        if return_sources:
            for c in meta.grounding_chunks or []:
                if c.web:
                    sources.append({"url": c.web.uri or "", "title": c.web.title or ""})
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
