"""
板块资金流向采集器
主：东财 stock_sector_fund_flow_rank（容易被 push2.eastmoney.com 封）
备：同花顺 stock_fund_flow_industry（90条，字段略有不同）
"""
from datetime import date
from loguru import logger
from storage.duckdb_store import upsert_many
from collector.trade_calendar import get_last_trading_date
import akshare as ak


def _fetch_eastmoney() -> list:
    """东财接口，返回标准化 rows"""
    df = ak.stock_sector_fund_flow_rank(indicator="今日")
    rows = []
    for _, row in df.iterrows():
        net_inflow_raw = float(row.get("今日主力净流入-净额", 0) or 0)
        rows.append({
            "sector_name":    str(row.get("名称", "")),
            "change_pct":     float(row.get("今日涨跌幅", 0) or 0),
            "main_net_inflow": round(net_inflow_raw / 1e8, 4),  # 元→亿元
            "total_volume":   0.0,
            "up_count":       0,
            "down_count":     0,
        })
    logger.info(f"板块资金(东财) {len(rows)} 条")
    return rows


def _fetch_ths() -> list:
    """同花顺备用接口，返回标准化 rows（净额单位已是亿元）"""
    df = ak.stock_fund_flow_industry(symbol="即时")
    rows = []
    for _, row in df.iterrows():
        rows.append({
            "sector_name":    str(row.get("行业", "")),
            "change_pct":     float(row.get("行业-涨跌幅", 0) or 0),
            "main_net_inflow": float(row.get("净额", 0) or 0),  # 已是亿元
            "total_volume":   float(row.get("流入资金", 0) or 0) + float(row.get("流出资金", 0) or 0),
            "up_count":       0,
            "down_count":     0,
        })
    logger.info(f"板块资金(同花顺备用) {len(rows)} 条")
    return rows


def fetch_sector_flow(trade_date: date = None) -> bool:
    if trade_date is None:
        trade_date = get_last_trading_date()
    logger.info(f"采集板块资金流向: {trade_date}")

    rows = []
    try:
        rows = _fetch_eastmoney()
    except Exception as e:
        logger.warning(f"板块资金东财失败，切换同花顺: {e}")
        try:
            rows = _fetch_ths()
        except Exception as e2:
            logger.error(f"板块资金同花顺也失败: {e2}")
            return False

    if not rows:
        logger.warning("板块资金流向：0条，跳过写入")
        return False

    for r in rows:
        r["date"] = trade_date

    upsert_many("sector_flow", rows)
    logger.info(f"板块资金流向写入 {len(rows)} 条")
    return True


def run(trade_date: date = None):
    fetch_sector_flow(trade_date)
