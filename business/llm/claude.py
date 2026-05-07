"""
慧盘 · Claude API 调用
用于：深度市场解读、综合信号分析（按需调用，不做日常自动任务）

需要在 config.py 或环境变量里设置：
  ANTHROPIC_API_KEY=sk-ant-xxx
"""

import os
import requests
from loguru import logger

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"


def call(
    prompt: str,
    system: str = None,
    max_tokens: int = 1000,
) -> str:
    """
    调用 Claude API，返回生成文本
    失败时返回空字符串
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY 未设置，跳过 Claude 调用")
        return ""

    messages = [{"role": "user", "content": prompt}]

    try:
        payload = {
            "model": DEFAULT_MODEL,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = system

        resp = requests.post(
            ANTHROPIC_BASE_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()

    except Exception as e:
        logger.error(f"Claude 调用失败: {e}")
        return ""


def analyze_market(summary: dict, signals: dict) -> str:
    """
    深度市场解读：结合今日数据和信号，生成分析段落
    summary: 来自 business/engine/summary.get_market_summary()
    signals: 来自 business/engine/signal.run_all_signals()
    """
    conclusions = "\n".join([c["conclusion"] for c in summary.get("conclusions", [])])
    risk_signals = "\n".join([f"- {s['name']}: {s['desc']}" for s in signals.get("risk_signals", [])])
    opp_signals = "\n".join([f"- {s['name']}: {s['desc']}" for s in signals.get("opportunity_signals", [])])

    system = "你是一位专业的A股市场分析师，擅长从数据中提炼洞察，语言简洁，不说废话，不做股票推荐。"

    prompt = f"""今日市场数据（{summary.get('date', '')}）：

【情绪分】{summary.get('sentiment_score', 0)}/100，{summary.get('headline', '')}

【核心指标分位数】
{conclusions}

【风险信号】
{risk_signals or '无'}

【机会信号】
{opp_signals or '无'}

请基于以上数据，用150字以内写一段今日市场解读，重点指出：当前所处的市场阶段、值得关注的异常点、短期需要警惕或关注的方向。不要重复数据，直接给出判断。"""

    return call(prompt, system=system, max_tokens=300)
