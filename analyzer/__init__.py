"""
Analyzer 包：基于真实抓取原文做合规分析。

包内子模块：
  main          主分析路径：scraped_content → compliance_analysis（无 grounding）
  fallback      降级合成：抓取失败/导航页时用 grounded fetch 合成内容后分析
  consolidation Stage 3 整合去重：reg_id 跨域合并 + LLM 同主题语义合并
  values        字段构造与 DB 写入（main 与 fallback 共用）
  backfill      历史字段补填 + 重置不相关条目
  _shared       共享常量、prompts、helper

对外 API：
  run_analysis        主入口（gris.py run / analyze 命令）
  requeue_irrelevant  重置「不相关」条目并待重新分析
"""
from .main import run_analysis
from .backfill import requeue_irrelevant

__all__ = ["run_analysis", "requeue_irrelevant"]
