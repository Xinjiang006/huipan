"""
资金流向采集器（北向）
接口：ak.stock_hsgt_hist_em(symbol='北向资金')
"""
import akshare as ak
import pandas as pd
from datetime import date
from loguru import logger
from storage.duckdb_store import upsert, query
from collector.trade_calendar import get_last_trading_date


def fetch_northbound(trade_date: date = None) -> bool:
    if trade_date is None:
        trade_date = get_last_trading_date()

    logger.info(f"采集北向资金: {trade_date}")
    try:
        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        df["日期"] = pd.to_datetime(df["日期"]).dt.date

        # 找最近有数据的交易日
        row = df[df["日期"] == trade_date]
        if row.empty or pd.isna(row.iloc[0].get("当日成交净买额")):
            # 取最近一条有效数据
            valid = df.dropna(subset=["沪深300"])
            if valid.empty:
                logger.warning("北向资金无有效数据")
                return False
            row = valid.iloc[-1:]
            trade_date = row.iloc[0]["日期"]
            logger.info(f"使用最近有效日期: {trade_date}")

        net = float(row.iloc[0].get("当日成交净买额", 0) or 0)

        existing = query("SELECT * FROM money_flow WHERE date = ?", [trade_date])
        record = {
            "date": trade_date,
            "north_sh_net": 0.0,
            "north_sz_net": 0.0,
            "north_total_net": net,
            "main_net_inflow": float(existing.iloc[0]["main_net_inflow"]) if not existing.empty else 0.0,
            "retail_net_inflow": float(existing.iloc[0]["retail_net_inflow"]) if not existing.empty else 0.0,
            "margin_balance": float(existing.iloc[0]["margin_balance"]) if not existing.empty else 0.0,
        }
        upsert("money_flow", record, pk=["date"])
        logger.info(f"北向资金净流入: {net:.1f}亿")
        return True
    except Exception as e:
        logger.error(f"北向资金采集失败: {e}")
        return False


def fetch_northbound_history() -> bool:
    logger.info("采集北向资金历史...")
    try:
        df = ak.stock_hsgt_hist_em(symbol="北向资金")
        df["日期"] = pd.to_datetime(df["日期"]).dt.date
        df = df.dropna(subset=["沪深300"])

        for _, row in df.iterrows():
            d = row["日期"]
            net = float(row.get("当日成交净买额", 0) or 0)
            existing = query("SELECT date FROM money_flow WHERE date = ?", [d])
            if existing.empty:
                upsert("money_flow", {
                    "date": d,
                    "north_sh_net": 0.0,
                    "north_sz_net": 0.0,
                    "north_total_net": net,
                    "main_net_inflow": 0.0,
                    "retail_net_inflow": 0.0,
                    "margin_balance": 0.0,
                }, pk=["date"])

        logger.info(f"北向历史写入完成，共 {len(df)} 条")
        return True
    except Exception as e:
        logger.error(f"北向历史采集失败: {e}")
        return False


def run(trade_date: date = None):
    fetch_northbound(trade_date)
