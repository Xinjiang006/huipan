"""
慧盘 · LLM 调用统一网关
所有 LLM 调用都经过这里，方便以后切换模型/接入 OpenClaw

分工：
  - Deepseek V3：日常自动任务（新闻摘要/翻译/异动关联），成本低
  - Claude API：复杂分析（深度市场解读/综合信号），按需调用

OpenClaw 预留：把 PROVIDER = "openclaw" 接进来时，只改这个文件
"""

import os
from enum import Enum
from loguru import logger


class LLMProvider(str, Enum):
    DEEPSEEK = "deepseek"
    CLAUDE = "claude"
    OPENCLAW = "openclaw"   # 预留


class LLMTask(str, Enum):
    # Deepseek 处理的日常任务
    NEWS_SUMMARY = "news_summary"          # 新闻摘要
    NEWS_TAG = "news_tag"                  # 板块标签提取
    ANOMALY_LINK = "anomaly_link"          # 异动关联分析
    # Claude 处理的复杂任务
    MARKET_ANALYSIS = "market_analysis"   # 深度市场解读
    SIGNAL_EXPLAIN = "signal_explain"     # 综合信号解读


# 任务 → 模型路由表（改这里来调整分工）
TASK_ROUTING: dict[LLMTask, LLMProvider] = {
    LLMTask.NEWS_SUMMARY:    LLMProvider.DEEPSEEK,
    LLMTask.NEWS_TAG:        LLMProvider.DEEPSEEK,
    LLMTask.ANOMALY_LINK:    LLMProvider.DEEPSEEK,
    LLMTask.MARKET_ANALYSIS: LLMProvider.CLAUDE,
    LLMTask.SIGNAL_EXPLAIN:  LLMProvider.CLAUDE,
}


def call(
    task: LLMTask,
    prompt: str,
    system: str = None,
    max_tokens: int = 800,
    provider: LLMProvider = None,   # 强制指定模型时传入，否则按路由
) -> str:
    """
    统一 LLM 调用入口
    返回模型生成的文本，失败时返回空字符串
    """
    provider = provider or TASK_ROUTING.get(task, LLMProvider.DEEPSEEK)
    logger.debug(f"LLM call: task={task}, provider={provider}")

    try:
        if provider == LLMProvider.DEEPSEEK:
            from business.llm.deepseek import call as deepseek_call
            return deepseek_call(prompt, system=system, max_tokens=max_tokens)

        elif provider == LLMProvider.CLAUDE:
            from business.llm.claude import call as claude_call
            return claude_call(prompt, system=system, max_tokens=max_tokens)

        elif provider == LLMProvider.OPENCLAW:
            # TODO: OpenClaw 接入后在这里实现
            raise NotImplementedError("OpenClaw 尚未接入")

        else:
            raise ValueError(f"未知 provider: {provider}")

    except Exception as e:
        logger.error(f"LLM 调用失败 task={task} provider={provider}: {e}")
        return ""
