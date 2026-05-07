"""
慧盘 · 一句话结论生成器（规则引擎）
基于当日数据 + 分位数，生成人话结论。
不依赖 LLM，纯规则，快且稳定。LLM 深度解读在 business/llm/ 里。

输出格式：
  涨停 44支 ↓ 近30日偏少，仅高于8个交易日
"""

import sys
sys.path.insert(0, ".")

from api.internal import get_market_latest, get_northbound_latest
from business.engine.percentile import MarketPercentileEngine


def get_market_summary(lookback_days: int = 30) -> dict:
    """
    生成当日市场核心指标的所有结论
    返回：{
        date: ...,
        conclusions: [
            {"metric": "limit_up", "conclusion": "涨停 44支 ↓ ..."},
            ...
        ],
        headline: "整体情绪偏弱，缩量下跌",  # 顶部一句话
    }
    """
    today = get_market_latest()
    if not today:
        return {"error": "无最新数据，请先运行采集"}

    engine = MarketPercentileEngine(lookback_days=lookback_days)
    conclusions = engine.get_all(today)

    # 生成顶部 headline（规则）
    headline = _gen_headline(today, conclusions)

    return {
        "date": str(today.get("date", "")),
        "sentiment_score": today.get("sentiment_score", 0),
        "conclusions": conclusions,
        "headline": headline,
    }


def _gen_headline(today: dict, conclusions: list[dict]) -> str:
    """基于规则生成一句话市场总结"""
    score = today.get("sentiment_score", 50)
    lu = today.get("limit_up_count", 0)
    ld = today.get("limit_down_count", 0)
    vol = today.get("total_volume", 0)

    # 从分位数结论里找成交额和涨停的相对位置
    vol_rank_pct = 50.0
    lu_rank_pct = 50.0
    for c in conclusions:
        if c["metric"] == "volume" and c["total"] > 0:
            vol_rank_pct = c["rank"] / c["total"] * 100
        if c["metric"] == "limit_up" and c["total"] > 0:
            lu_rank_pct = c["rank"] / c["total"] * 100

    # 情绪定性
    if score >= 70:
        mood = "情绪偏强"
    elif score >= 40:
        mood = "情绪中性"
    else:
        mood = "情绪偏弱"

    # 量能定性
    if vol_rank_pct >= 70:
        vol_desc = "放量"
    elif vol_rank_pct <= 30:
        vol_desc = "缩量"
    else:
        vol_desc = "量能平稳"

    # 涨停定性
    if lu_rank_pct >= 70:
        limit_desc = f"涨停活跃（{lu}支）"
    elif lu_rank_pct <= 30:
        limit_desc = f"涨停稀少（{lu}支）"
    else:
        limit_desc = f"涨停正常（{lu}支）"

    return f"{mood}，{vol_desc}，{limit_desc}"


if __name__ == "__main__":
    result = get_market_summary()
    if "error" in result:
        print(result["error"])
    else:
        print(f"\n【{result['date']}】情绪分 {result['sentiment_score']}")
        print(f"→ {result['headline']}\n")
        for c in result["conclusions"]:
            print(f"  {c['conclusion']}")
