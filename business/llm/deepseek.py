"""
慧盘 · Deepseek V3 调用
用于：新闻摘要、板块标签提取、异动关联（日常自动任务，成本低）

需要在 config.py 或环境变量里设置：
  DEEPSEEK_API_KEY=sk-xxx
"""

import os
import json
import requests
from loguru import logger

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"   # = DeepSeek-V3


def call(
    prompt: str,
    system: str = None,
    max_tokens: int = 800,
    temperature: float = 0.3,
) -> str:
    """
    调用 Deepseek V3，返回生成文本
    失败时返回空字符串，不抛异常（保证主流程不中断）
    """
    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY 未设置，跳过 LLM 调用")
        return ""

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEFAULT_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    except Exception as e:
        logger.error(f"Deepseek 调用失败: {e}")
        return ""


def summarize_news(news_list: list[dict]) -> str:
    """
    新闻摘要：把多条新闻压缩成 3 句话
    news_list: [{"title": ..., "time": ...}, ...]
    """
    if not news_list:
        return ""

    news_text = "\n".join(
        [f"[{n.get('time', '')}] {n.get('title', '')}" for n in news_list[:20]]
    )
    prompt = f"""以下是今日A股相关新闻，请用3句话提炼最重要的信息，重点关注政策、资金、板块异动，避免标题党：

{news_text}

要求：
1. 每句话不超过40字
2. 只提炼实质性信息，不要重复标题
3. 输出格式：① ... ② ... ③ ..."""

    return call(prompt, max_tokens=200)


def tag_news_sectors(title: str) -> list[str]:
    """
    从新闻标题提取受益板块标签
    返回板块列表，最多 3 个
    """
    prompt = f"""从以下新闻标题中提取受益的A股板块，最多3个，只输出板块名称，用逗号分隔，没有相关板块则输出"无"：

{title}"""

    result = call(prompt, max_tokens=30, temperature=0.1)
    if not result or result == "无":
        return []
    return [s.strip() for s in result.split(",") if s.strip()]
