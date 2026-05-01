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


def _price_for_model(model: str | None) -> tuple[float, float]:
    """返回 (input_price_per_M, output_price_per_M)；未识别模型回退默认。"""
    if not model:
        return PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M
    p = _MODEL_PRICES.get(model)
    if p:
        return p["in"], p["out"]
    # 模糊匹配：把 "gemini-2.5-flash-001" 匹配到 "gemini-2.5-flash"
    for key, p in _MODEL_PRICES.items():
        if model.startswith(key):
            return p["in"], p["out"]
    return PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M


_token_stats: dict = {
    "calls":           0,
    "grounded_calls":  0,
    "input_tokens":    0,
    "output_tokens":   0,
    "thoughts_tokens": 0,   # 单独追踪 thinking — 让 print 能展示比例
    "by_label":        {},  # label → {calls, in, out}
    "by_model":        {},  # model → {calls, in, out, thoughts, grounded}
}


def _record_tokens(label: str, resp, grounded: bool, model: str = "") -> None:
    """从 SDK response 提取 token usage 累计到 _token_stats。

    关键：Gemini 2.5 系列是 thinking model，定价表"Output price (including
    thinking tokens)"——真实计费输出 = candidates + thoughts。
    同时按 model 分桶累计，让成本估算用对应模型单价（之前一刀切 Flash 单价
    导致 lite 模型估算偏高 6 倍）。
    """
    try:
        usage = getattr(resp, "usage_metadata", None)
        if not usage:
            return
        in_tok       = getattr(usage, "prompt_token_count", 0) or 0
        cand_tok     = getattr(usage, "candidates_token_count", 0) or 0
        thoughts_tok = getattr(usage, "thoughts_token_count", 0) or 0
        out_tok      = cand_tok + thoughts_tok
        with _token_lock:
            _token_stats["calls"]           += 1
            _token_stats["input_tokens"]    += in_tok
            _token_stats["output_tokens"]   += out_tok
            _token_stats["thoughts_tokens"] += thoughts_tok
            if grounded:
                _token_stats["grounded_calls"] += 1
            bl = _token_stats["by_label"].setdefault(
                label, {"calls": 0, "in": 0, "out": 0, "grounded": 0},
            )
            bl["calls"]    += 1
            bl["in"]       += in_tok
            bl["out"]      += out_tok
            bl["grounded"] += 1 if grounded else 0
            mb = _token_stats["by_model"].setdefault(
                model or "unknown",
                {"calls": 0, "in": 0, "out": 0, "thoughts": 0, "grounded": 0},
            )
            mb["calls"]    += 1
            mb["in"]       += in_tok
            mb["out"]      += out_tok
            mb["thoughts"] += thoughts_tok
            mb["grounded"] += 1 if grounded else 0
        _log.info("TOKENS %-15s model=%s in=%d out=%d (cand=%d think=%d) grounded=%s",
                  label, model or "?", in_tok, out_tok, cand_tok, thoughts_tok, grounded)
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
            "by_model":       {k: dict(v) for k, v in _token_stats["by_model"].items()},
        }


def reset_token_stats() -> None:
    """重置统计；通常在一次 run 开始时调用。"""
    with _token_lock:
        _token_stats["calls"]           = 0
        _token_stats["grounded_calls"]  = 0
        _token_stats["input_tokens"]    = 0
        _token_stats["output_tokens"]   = 0
        _token_stats["thoughts_tokens"] = 0
        _token_stats["by_label"].clear()
        _token_stats["by_model"].clear()


# ── 定价常量（Standard 付费 Tier；以官方文档为准） ────────────────────────────
# 参考：https://ai.google.dev/gemini-api/docs/pricing  （核对于 2026-04）
# 不同模型分别计费——之前一刀切用 Flash 单价导致 lite 模型估算偏高 6 倍。
_MODEL_PRICES = {
    # gemini-2.5-flash
    "gemini-2.5-flash":              {"in": 0.30, "out": 2.50},
    # gemini-2.5-flash-lite (输出价含 thinking tokens)
    "gemini-2.5-flash-lite":         {"in": 0.10, "out": 0.40},
    "gemini-2.5-flash-lite-preview-09-2025": {"in": 0.10, "out": 0.40},
    # gemini-2.5-pro (≤200k tokens 价；超 200k 翻倍，这里按多数场景估)
    "gemini-2.5-pro":                {"in": 1.25, "out": 10.0},
    # gemini-3.1-pro-preview
    "gemini-3.1-pro-preview":        {"in": 2.00, "out": 12.0},
    # gemini-3.1-flash-lite-preview
    "gemini-3.1-flash-lite-preview": {"in": 0.25, "out": 1.50},
    # gemini-3-flash-preview
    "gemini-3-flash-preview":        {"in": 0.50, "out": 3.00},
}

# 默认 fallback 单价（若模型不在表中按 Flash 估）
PRICE_INPUT_PER_M    = 0.30
PRICE_OUTPUT_PER_M   = 2.50

# Grounding Search 计费
# Gemini 2.5: 1500 RPD 免费（Flash + Flash-Lite 共享），超额 $35/1000
# Gemini 3:   5000/月 免费（Gemini 3 共享），超额 $14/1000
GROUNDING_FREE_RPD       = 1500
GROUNDING_PRICE_PER_K    = 35.0
GROUNDING_PRICE_PER_K_G3 = 14.0


def print_token_summary(prefix: str = "") -> None:
    """末尾汇总打印——按 label/model 分组显示 token 消耗 + 按模型分别计费。"""
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

    # 按模型分别计费（修正"一刀切 Flash 单价"导致的偏差）
    # 同时显示 thinking 占比 — 帮用户识别"哪些调用 thinking 占比高 = 浪费"
    total_cost = 0.0
    total_thoughts = s.get("thoughts_tokens", 0)
    print(f"\n  按模型计费：")
    for model, bm in sorted(s["by_model"].items(), key=lambda x: -x[1]["in"] - x[1]["out"]):
        in_p, out_p = _price_for_model(model)
        in_c  = bm["in"]  / 1_000_000 * in_p
        out_c = bm["out"] / 1_000_000 * out_p
        total_cost += in_c + out_c
        thoughts = bm.get("thoughts", 0)
        cand = bm["out"] - thoughts
        ratio = (thoughts / bm["out"] * 100) if bm["out"] > 0 else 0
        think_tag = f" [thinking {thoughts:,}/{bm['out']:,} = {ratio:.0f}%]" if thoughts else ""
        print(f"    {model:<35} calls={bm['calls']:<4} "
              f"in={bm['in']:>8,}×${in_p}=${in_c:.3f}  "
              f"out={bm['out']:>8,}×${out_p}=${out_c:.3f}{think_tag}")
    if total_thoughts > 0:
        print(f"\n  💡 thinking tokens 总计 {total_thoughts:,}（占输出 "
              f"{total_thoughts / s['output_tokens'] * 100:.0f}%）— "
              f"对结构化模板填/简单事实查询场景可设 thinking_budget=0 节省")

    paid_grounded = max(0, s["grounded_calls"] - GROUNDING_FREE_RPD)
    g_cost = paid_grounded / 1000 * GROUNDING_PRICE_PER_K
    total_cost += g_cost

    print(f"\n  估算成本：${total_cost:.4f}")
    print(f"    └ grounding: ${g_cost:.3f}  ({s['grounded_calls']} 次,免费 {GROUNDING_FREE_RPD}/天后 ${GROUNDING_PRICE_PER_K}/1k)")
    print()


def estimate_cost_usd(stats: dict | None = None) -> float:
    """按各模型实际单价估算总成本（美元）。

    重要修正：Gemini 2.5 Flash 是重型 thinking model。
    JSON 调用里 candidates_token_count 可能极小（1-100，仅 JSON 输出本身），
    但 thoughts_token_count 高达几千。定价表"Output price (including
    thinking tokens)" — 真实计费输出 = cand + thinking。

    新调用（_record_tokens 已记录 cand+thinking 之和到 out）：估算准确。
    历史调用（旧 _record_tokens 只记 cand）：output 严重低估，需补偿。

    校准依据：用户实测一次完整 run（4-30, 772 calls, 76 分钟）= $17
      cand-only output = 1.79M, input = 2.73M
      反推 thinking total = 4.68M  →  thinking : cand ≈ 2.61
      → 真实 output ≈ cand × 3.61
    """
    s = stats or get_token_stats()
    total = 0.0
    for model, bm in (s.get("by_model") or {}).items():
        in_p, out_p = _price_for_model(model)
        total += bm["in"]  / 1_000_000 * in_p
        total += bm["out"] / 1_000_000 * out_p
    if not s.get("by_model"):
        # 旧 stats 没 by_model：按 Flash 估，且对 output 加 thinking 补偿系数 3.61
        # （历史 _record_tokens 只记 candidates，漏算 thoughts ≈ 2.61 × cand）
        total = (s["input_tokens"]  / 1_000_000 * PRICE_INPUT_PER_M
                 + s["output_tokens"] * 3.61 / 1_000_000 * PRICE_OUTPUT_PER_M)
    paid_grounded = max(0, s["grounded_calls"] - GROUNDING_FREE_RPD)
    total += paid_grounded / 1000 * GROUNDING_PRICE_PER_K
    return total


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
            _record_tokens(label, resp, grounded, model=model)
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


class BlockedResponseError(RuntimeError):
    """模型返回空响应且 finish_reason 非 STOP（SAFETY / RECITATION / MAX_TOKENS 等）。
    与"模型说了空字符串"区分开——前者是被屏蔽，调用方应当 fail-loud 而非静默入库。
    """


def _check_finish_reason(resp, label: str) -> None:
    """resp.text 为空时检查 finish_reason；非 STOP 则视为被屏蔽，抛错。
    SDK 在 SAFETY/RECITATION/MAX_TOKENS 时 text 都是 None——不区分会让被屏蔽的法规
    悄悄变成"信息不足→低置信猜测"入库，污染合规情报。
    """
    fr_str = ""
    try:
        fr = resp.candidates[0].finish_reason
        fr_str = str(fr) if fr is not None else ""
    except (IndexError, AttributeError):
        return
    if not fr_str:
        return
    # FinishReason 在不同 SDK 版本里可能是 enum 或字符串；统一按结尾匹配 "STOP"
    if fr_str.endswith("STOP") or fr_str.endswith("FINISH_REASON_STOP"):
        return
    _log.warning("%s blocked: finish_reason=%s", label, fr_str)
    raise BlockedResponseError(f"{label} blocked: finish_reason={fr_str}")


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
    thinking_budget: int | None = None,
) -> tuple[str, list[dict]]:
    """
    带 Google Search grounding 的调用。
    返回 (text, sources)。sources 为空列表表示无 grounding 数据。

    thinking_budget:
      None → 模型默认（Flash/Pro 默认开 thinking，输出价含 thinking tokens）
      0    → 关闭 thinking（适合简单事实查询，节省 ~62% 输出成本）
      N>0  → 限制最多 N 个 thinking tokens
      不支持 thinking 的模型（如 Pro 强制 thinking）会被 SDK 忽略此参数。

    return_sources=False 时跳过 sources 解析(节省一点点),仍返回元组以兼容签名。
    """
    cfg_kwargs = {
        "tools": [types.Tool(google_search=types.GoogleSearch())],
        "system_instruction": system,
        "temperature": temperature,
    }
    if top_p is not None:
        cfg_kwargs["top_p"] = top_p
    if thinking_budget is not None:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    cfg = types.GenerateContentConfig(**cfg_kwargs)

    resp = _do_call_with_retry(
        label="call_grounded", model=model, prompt=prompt, cfg=cfg, retries=retries,
        grounded=True,
    )

    text = resp.text or ""
    if not text:
        _check_finish_reason(resp, "call_grounded")
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
    thinking_budget: int | None = None,
) -> str:
    """
    无 grounding,要求 JSON 响应。
    返回原始 text(调用方负责 parse)。

    thinking_budget:
      None → 默认（Flash thinking model 全开，输出价含 thinking tokens）
      0    → 关 thinking（结构化模板填充类任务可关，节省 ~62% 输出成本）
      N>0  → 限制 thinking 上限
    """
    cfg_kwargs = {
        "system_instruction": system,
        "response_mime_type": "application/json",
        "temperature": temperature,
    }
    if thinking_budget is not None:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    cfg = types.GenerateContentConfig(**cfg_kwargs)
    resp = _do_call_with_retry(
        label="call_json", model=model, prompt=prompt, cfg=cfg, retries=retries,
    )
    text = resp.text or ""
    if not text:
        _check_finish_reason(resp, "call_json")
    return text
