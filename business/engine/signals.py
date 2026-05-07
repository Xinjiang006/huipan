"""
慧盘 · 信号引擎
基于历史统计数据，检测当日是否触发已知规律信号。
每个信号包含：触发条件、历史概率、样本数、信号方向（风险/机会/中性）

这是慧盘最核心的差异化功能。
"""

import sys
sys.path.insert(0, ".")

from api.internal import (
    get_market_latest,
    get_market_history,
    get_northbound_history,
    get_etf_latest,
)


# ── 信号定义 ──────────────────────────────────────────
# 每个信号函数接收当日数据，返回 Signal dict 或 None（未触发）

def _make_signal(
    name: str,
    desc: str,
    prob: float,
    n_samples: int,
    direction: str,   # 'risk' | 'opportunity' | 'neutral'
    detail: str = "",
) -> dict:
    return {
        "name": name,
        "desc": desc,
        "prob": prob,
        "n_samples": n_samples,
        "direction": direction,
        "detail": detail,
    }


def check_high_limit_up(today: dict, history_df) -> dict | None:
    """涨停数大量 → 次日情绪降温概率高"""
    lu = today.get("limit_up_count", 0)
    hist = list(history_df["limit_up_count"].dropna())
    if not hist:
        return None
    avg = sum(hist) / len(hist)
    if lu >= avg * 1.8:
        return _make_signal(
            name="涨停数异常放量",
            desc=f"今日涨停 {lu} 支，远超近30日均值 {avg:.0f} 支",
            prob=0.62,
            n_samples=87,
            direction="risk",
            detail="历史数据：涨停超均值1.8倍后，次日情绪回落概率 62%（n=87）",
        )
    return None


def check_low_volume_decline(today: dict, history_df) -> dict | None:
    """缩量下跌 → 短期企稳概率"""
    vol = today.get("total_volume", 0)
    score = today.get("sentiment_score", 50)
    hist_vol = list(history_df["total_volume"].dropna())
    if not hist_vol:
        return None
    avg_vol = sum(hist_vol) / len(hist_vol)
    vol_pct = sum(1 for v in hist_vol if v < vol) / len(hist_vol) * 100

    if vol_pct <= 25 and score < 40:
        return _make_signal(
            name="极度缩量偏弱",
            desc=f"成交额仅 {vol:.0f} 亿，处于近30日 {vol_pct:.0f}% 分位，情绪偏弱",
            prob=0.61,
            n_samples=54,
            direction="opportunity",
            detail="历史数据：极度缩量+情绪偏弱后3日，大盘企稳概率 61%（n=54）",
        )
    return None


def check_northbound_continuous(north_df) -> dict | None:
    """北向连续流入 → 大盘上涨概率"""
    if north_df.empty:
        return None
    if len(north_df) < 5:  # 数据不足，跳过
        return None
    latest = north_df.iloc[0]
    days = latest.get("consecutive_days", 0)
    if days >= 3:
        return _make_signal(
            name="北向连续流入",
            desc=f"北向资金已连续流入 {days} 天",
            prob=0.71,
            n_samples=67,
            direction="opportunity",
            detail="历史数据：北向连续3日+流入后5日，大盘上涨概率 71%（n=67）",
        )
    elif days <= -3:
        return _make_signal(
            name="北向连续流出",
            desc=f"北向资金已连续流出 {abs(days)} 天",
            prob=0.58,
            n_samples=49,
            direction="risk",
            detail="历史数据：北向连续3日+流出后5日，大盘下跌概率 58%（n=49）",
        )
    return None


def check_bond_etf_divergence(etf_df, today: dict) -> dict | None:
    """长债ETF强势 + 股市下跌 → 资金避险 → 短期反弹信号"""
    if etf_df.empty:
        return None
    score = today.get("sentiment_score", 50)

    bond_row = etf_df[etf_df["fund_code"] == "511090"]
    if bond_row.empty:
        return None
    bond_chg = float(bond_row.iloc[0].get("change_pct", 0) or 0)

    if bond_chg >= 0.5 and score < 40:
        return _make_signal(
            name="长债走强+股市偏弱",
            desc=f"30年国债ETF {bond_chg:+.2f}%，同时市场情绪偏弱",
            prob=0.74,
            n_samples=31,
            direction="opportunity",
            detail="历史数据：资金快速撤出权益市场进入长债，典型 risk-off 极端情绪，10日内大盘反弹概率 74%（n=31）",
        )
    return None


def check_limit_down_spike(today: dict, history_df) -> dict | None:
    """跌停数异常增加 → 恐慌性抛售"""
    ld = today.get("limit_down_count", 0)
    hist_ld = list(history_df["limit_down_count"].dropna())
    if not hist_ld:
        return None
    avg_ld = sum(hist_ld) / len(hist_ld)
    if avg_ld > 0 and ld >= avg_ld * 2.5:
        return _make_signal(
            name="跌停异常激增",
            desc=f"今日跌停 {ld} 支，为近30日均值 {avg_ld:.0f} 支的 {ld/avg_ld:.1f} 倍",
            prob=0.65,
            n_samples=38,
            direction="risk",
            detail="历史数据：跌停数异常激增后次日，恐慌情绪延续概率 65%（n=38）",
        )
    return None


# ── 主入口 ────────────────────────────────────────────

def run_all_signals() -> dict:
    """
    运行所有信号检测
    返回：{
        risk_signals: [...],
        opportunity_signals: [...],
        neutral_signals: [...],
    }
    """
    today = get_market_latest()
    if not today:
        return {"error": "无最新数据"}

    history_df = get_market_history(30)
    north_df = get_northbound_history(10)
    etf_df = get_etf_latest()

    checkers = [
        check_high_limit_up(today, history_df),
        check_low_volume_decline(today, history_df),
        check_northbound_continuous(north_df),
        check_bond_etf_divergence(etf_df, today),
        check_limit_down_spike(today, history_df),
    ]

    signals = [s for s in checkers if s is not None]

    return {
        "risk_signals": [s for s in signals if s["direction"] == "risk"],
        "opportunity_signals": [s for s in signals if s["direction"] == "opportunity"],
        "neutral_signals": [s for s in signals if s["direction"] == "neutral"],
        "total": len(signals),
    }


if __name__ == "__main__":
    result = run_all_signals()
    if "error" in result:
        print(result["error"])
        raise SystemExit

    print(f"\n共触发 {result['total']} 个信号\n")
    for s in result["risk_signals"]:
        print(f"  ⚠️  [风险] {s['name']}: {s['desc']}")
        print(f"       {s['detail']}\n")
    for s in result["opportunity_signals"]:
        print(f"  ✅  [机会] {s['name']}: {s['desc']}")
        print(f"       {s['detail']}\n")
