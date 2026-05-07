"""
慧盘 · 分位数计算引擎
把任意数值转换为"近30日处于第N高"的大白话结论

格式：涨停 44支 ↓ 近30日偏少，仅高于8个交易日
"""

import sys
sys.path.insert(0, ".")

import numpy as np
from api.internal import get_market_history


def calc_percentile(value: float, series: list[float]) -> dict:
    """
    计算 value 在 series 中的位置
    返回：{rank, total, pct, direction}
    rank：从低到高排第几（1=最低）
    """
    arr = np.array(series)
    rank = int(np.sum(arr < value)) + 1   # 高于几个值
    total = len(arr)
    pct = round(rank / total * 100, 1)
    return {"rank": rank, "total": total, "pct": pct}


def format_conclusion(label: str, value, unit: str, rank: int, total: int) -> str:
    """
    生成大白话结论
    格式：涨停 44支 ↓ 近30日偏少，仅高于8个交易日
    """
    # 偏高/偏低判断：rank > total*0.6 为偏高，< total*0.4 为偏低
    if rank >= total * 0.75:
        level = "偏高"
        arrow = "↑"
    elif rank <= total * 0.25:
        level = "偏少" if "停" in label or "家" in label else "偏低"
        arrow = "↓"
    else:
        level = "正常"
        arrow = "→"

    if isinstance(value, float):
        val_str = f"{value:.0f}" if value >= 100 else f"{value:.1f}"
    else:
        val_str = str(value)

    return f"{label} {val_str}{unit} {arrow} 近{total}日{level}，仅高于{rank - 1}个交易日"


class MarketPercentileEngine:
    """
    市场核心指标分位数引擎
    每次实例化时加载最近 N 日历史数据
    """

    def __init__(self, lookback_days: int = 30):
        self.days = lookback_days
        self._load()

    def _load(self):
        df = get_market_history(self.days)
        self._df = df
        self._limit_up = list(df["limit_up_count"].dropna())
        self._limit_down = list(df["limit_down_count"].dropna())
        self._volume = list(df["total_volume"].dropna())
        self._sentiment = list(df["sentiment_score"].dropna())

        # 涨跌比 = up_count / (up_count + down_count)
        up = df["up_count"].fillna(0)
        down = df["down_count"].fillna(0)
        total = up + down
        ratio = (up / total.replace(0, np.nan)).dropna()
        self._up_ratio = list(ratio)

    def get_all(self, today: dict) -> list[dict]:
        """
        传入今日数据 dict（来自 api/internal.get_market_latest()）
        返回所有指标的分位数结论列表
        """
        results = []

        # 1. 涨停数
        lu = today.get("limit_up_count", 0)
        if self._limit_up:
            r = calc_percentile(lu, self._limit_up)
            results.append({
                "metric": "limit_up",
                "label": "涨停",
                "value": lu,
                "unit": "支",
                "rank": r["rank"],
                "total": r["total"],
                "conclusion": format_conclusion("涨停", lu, "支", r["rank"], r["total"]),
            })

        # 2. 跌停数
        ld = today.get("limit_down_count", 0)
        if self._limit_down:
            r = calc_percentile(ld, self._limit_down)
            results.append({
                "metric": "limit_down",
                "label": "跌停",
                "value": ld,
                "unit": "支",
                "rank": r["rank"],
                "total": r["total"],
                "conclusion": format_conclusion("跌停", ld, "支", r["rank"], r["total"]),
            })

        # 3. 成交额
        vol = today.get("total_volume", 0.0)
        if self._volume:
            r = calc_percentile(vol, self._volume)
            results.append({
                "metric": "volume",
                "label": "成交额",
                "value": vol,
                "unit": "亿",
                "rank": r["rank"],
                "total": r["total"],
                "conclusion": format_conclusion("成交额", vol, "亿", r["rank"], r["total"]),
            })

        # 4. 涨跌比
        up = today.get("up_count", 0)
        down = today.get("down_count", 0)
        if up + down > 0 and self._up_ratio:
            ratio = up / (up + down)
            r = calc_percentile(ratio, self._up_ratio)
            ratio_pct = round(ratio * 100, 1)
            results.append({
                "metric": "up_ratio",
                "label": "上涨比例",
                "value": ratio_pct,
                "unit": "%",
                "rank": r["rank"],
                "total": r["total"],
                "conclusion": format_conclusion("上涨比例", ratio_pct, "%", r["rank"], r["total"]),
            })

        return results


if __name__ == "__main__":
    from api.internal import get_market_latest
    today = get_market_latest()
    if today:
        engine = MarketPercentileEngine(lookback_days=30)
        for item in engine.get_all(today):
            print(item["conclusion"])
    else:
        print("无数据，请先运行 init_history.py")
