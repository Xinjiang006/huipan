from __future__ import annotations
"""
慧盘 · 数据层内部接口
业务层通过此模块取数，不直接查 DuckDB。
好处：以后换数据源只改这里，业务层代码不用动。
"""

from datetime import date
from typing import Optional
import pandas as pd
from storage.duckdb_store import query


# ── 市场情绪 ──────────────────────────────────────────

def get_market_latest() -> dict | None:
    """获取最新一个交易日的市场情绪数据"""
    df = query(
        "SELECT * FROM market_sentiment ORDER BY date DESC LIMIT 1"
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def get_market_history(days: int = 30) -> pd.DataFrame:
    """
    获取最近 N 个交易日的市场情绪历史（用于分位数计算）
    返回列：date, limit_up_count, limit_down_count, up_count, down_count,
            total_volume, sentiment_score
    """
    return query(
        "SELECT * FROM market_sentiment ORDER BY date DESC LIMIT ?",
        [days]
    )


def get_market_by_date(d: date) -> dict | None:
    """获取指定日期的市场情绪"""
    df = query("SELECT * FROM market_sentiment WHERE date = ?", [d])
    if df.empty:
        return None
    return df.iloc[0].to_dict()


# ── 板块资金 ──────────────────────────────────────────

def get_sector_latest(top_n: int = 20) -> pd.DataFrame:
    """获取最新交易日板块资金流向，涨跌各取一半确保热力图红绿混合"""
    latest_date = query(
        "SELECT MAX(date) as d FROM sector_flow"
    ).iloc[0]["d"]
    df = query(
        """SELECT * FROM sector_flow WHERE date = ?""",
        [latest_date]
    )
    if df.empty:
        return df
    half = max(4, top_n // 2)
    up   = df[df.change_pct > 0].nlargest(half, 'change_pct')
    down = df[df.change_pct < 0].nsmallest(half, 'change_pct')
    result = pd.concat([up, down]).sort_values('change_pct', key=abs, ascending=False)
    return result.head(top_n)


def get_sector_history(sector_name: str, days: int = 30) -> pd.DataFrame:
    """获取单个板块的历史资金流向"""
    return query(
        """SELECT * FROM sector_flow
           WHERE sector_name = ?
           ORDER BY date DESC LIMIT ?""",
        [sector_name, days]
    )


# ── ETF ───────────────────────────────────────────────

def get_etf_latest() -> pd.DataFrame:
    """获取最新交易日所有跟踪 ETF 的快照"""
    latest_date = query(
        "SELECT MAX(date) as d FROM etf_snapshot"
    ).iloc[0]["d"]
    return query(
        "SELECT * FROM etf_snapshot WHERE date = ? ORDER BY category, fund_name",
        [latest_date]
    )


def get_etf_share_change(fund_code: str, days: int = 5) -> pd.DataFrame:
    """获取某 ETF 最近 N 日份额变化（资金先行指标）"""
    return query(
        """SELECT date, share_change, total_share, change_pct
           FROM etf_snapshot
           WHERE fund_code = ?
           ORDER BY date DESC LIMIT ?""",
        [fund_code, days]
    )


# ── 北向资金 ──────────────────────────────────────────

def get_northbound_latest() -> dict | None:
    """获取最近有效交易日北向资金数据（跳过全零/NaN的非交易日）"""
    df = query(
        """SELECT * FROM money_flow 
           WHERE north_total_net IS NOT NULL 
             AND north_total_net != 0
           ORDER BY date DESC LIMIT 1"""
    )
    if df.empty:
        # 降级：取最新一行
        df = query("SELECT * FROM money_flow ORDER BY date DESC LIMIT 1")
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def get_northbound_history(days: int = 30) -> pd.DataFrame:
    """获取北向资金历史（连续流入天数 + 净流入金额）"""
    return query(
        "SELECT * FROM money_flow ORDER BY date DESC LIMIT ?",
        [days]
    )


# ── 涨停明细 ──────────────────────────────────────────

def get_limit_up_latest(limit: int = 50) -> pd.DataFrame:
    """获取最新交易日涨停股明细"""
    latest_date = query(
        "SELECT MAX(date) as d FROM limit_up_records"
    ).iloc[0]["d"]
    return query(
        """SELECT * FROM limit_up_records
           WHERE date = ?
           ORDER BY consecutive_days DESC
           LIMIT ?""",
        [latest_date, limit]
    )


def get_limit_up_consecutive(min_days: int = 2) -> pd.DataFrame:
    """获取当前连续涨停 N 板以上的股票"""
    latest_date = query(
        "SELECT MAX(date) as d FROM limit_up_records"
    ).iloc[0]["d"]
    return query(
        """SELECT * FROM limit_up_records
           WHERE date = ? AND continuous_limit_up_days >= ?
           ORDER BY continuous_limit_up_days DESC""",
        [latest_date, min_days]
    )


# ── 交易日历 ──────────────────────────────────────────

def get_last_n_trading_dates(n: int = 30) -> list[date]:
    """获取最近 N 个交易日的日期列表"""
    df = query(
        """SELECT date FROM trade_calendar
           WHERE is_trading = true AND date <= CURRENT_DATE
           ORDER BY date DESC LIMIT ?""",
        [n]
    )
    return list(df["date"])
