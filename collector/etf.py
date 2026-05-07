"""
ETF 快照采集器
- 接口：ak.fund_etf_fund_info_em(fund=code)
- 只取最近3条，避免拉全量历史数据（3000+条太慢）
"""
import akshare as ak
import pandas as pd
from datetime import date
from loguru import logger
from storage.duckdb_store import upsert_many
from collector.trade_calendar import get_last_trading_date

TRACKED_ETFS = [
    ("510300", "沪深300ETF", "宽基"),
    ("510500", "中证500ETF", "宽基"),
    ("159915", "创业板ETF", "宽基"),
    ("588000", "科创50ETF", "宽基"),
    ("512880", "证券ETF", "行业"),
    ("512690", "酒ETF", "行业"),
    ("512010", "医疗ETF", "行业"),
    ("512760", "芯片ETF", "行业"),
    ("159928", "消费ETF", "行业"),
    ("511010", "国债ETF", "债券"),
    ("511090", "30年国债ETF", "债券"),
]


def fetch_etf_snapshot(trade_date: date = None) -> bool:
    if trade_date is None:
        trade_date = get_last_trading_date()

    logger.info(f"采集 ETF 快照: {trade_date}")
    rows = []
    for code, name, category in TRACKED_ETFS:
        try:
            df = ak.fund_etf_fund_info_em(fund=code)
            if df is None or df.empty:
                continue

            # 只取最近3条，不需要全量历史
            df = df.sort_values("净值日期", ascending=False).head(3).reset_index(drop=True)

            latest = df.iloc[0]
            change_pct = float(latest.get("日增长率", 0) or 0)
            total_share = float(latest.get("份额", 0) or 0) / 1e8

            share_change = 0.0
            if len(df) >= 2:
                prev_share = float(df.iloc[1].get("份额", 0) or 0) / 1e8
                share_change = total_share - prev_share

            rows.append({
                "date": trade_date,
                "fund_code": code,
                "fund_name": name,
                "change_pct": change_pct,
                "share_change": share_change,
                "total_share": total_share,
                "total_size": 0.0,
                "category": category,
            })
            logger.debug(f"ETF {code} {name}: 涨跌{change_pct:.2f}% 份额变化{share_change:.4f}亿")
        except Exception as e:
            logger.error(f"ETF {code} 失败: {e}")

    if rows:
        upsert_many("etf_snapshot", rows)
        logger.info(f"ETF 快照写入 {len(rows)} 条")
    return True


def run(trade_date: date = None):
    fetch_etf_snapshot(trade_date)
