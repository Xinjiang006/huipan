"""
大盘情绪 + 涨跌停采集器
业务逻辑层：调用 adapters/market_data.py，处理数据，写入 DuckDB
"""
from datetime import date
from loguru import logger
from storage.duckdb_store import upsert_many, upsert, query
from collector.trade_calendar import get_last_trading_date
from collector.adapters.market_data import (
    get_limit_up_pool,
    get_limit_up_broken_pool,
    get_limit_down_pool,
    get_market_activity,
)


def fetch_limit_up(trade_date: date = None) -> bool:
    """采集涨停池，写入 limit_up_records"""
    if trade_date is None:
        trade_date = get_last_trading_date()
    date_str = trade_date.strftime("%Y%m%d")

    logger.info(f"采集涨停池: {date_str}")
    df = get_limit_up_pool(date_str)
    if df is None or df.empty:
        logger.warning("涨停池数据为空")
        return False

    rows = []
    for _, row in df.iterrows():
        rows.append({
            "date": trade_date,
            "stock_code": str(row.get("代码", "")).zfill(6),
            "stock_name": str(row.get("名称", "")),
            "consecutive_days": int(row.get("连板数", 1) or 1),
            "is_broken": False,
            "open_pct": float(row.get("竞价涨幅", 0) or 0),
            "sector": str(row.get("板块", "")),
            "limit_up_time": str(row.get("最终涨停时间", "")),
        })

    if rows:
        upsert_many("limit_up_records", rows)
        logger.info(f"涨停池写入 {len(rows)} 条")
    return True


def fetch_limit_up_broken(trade_date: date = None) -> bool:
    """采集炸板池（涨停后跌落），写入 limit_up_records"""
    if trade_date is None:
        trade_date = get_last_trading_date()
    date_str = trade_date.strftime("%Y%m%d")

    logger.info(f"采集炸板池: {date_str}")
    df = get_limit_up_broken_pool(date_str)
    if df is None or df.empty:
        logger.warning("炸板池数据为空")
        return False

    rows = []
    for _, row in df.iterrows():
        rows.append({
            "date": trade_date,
            "stock_code": str(row.get("代码", "")).zfill(6),
            "stock_name": str(row.get("名称", "")),
            "consecutive_days": int(row.get("连板数", 1) or 1),
            "is_broken": True,
            "open_pct": float(row.get("竞价涨幅", 0) or 0),
            "sector": str(row.get("板块", "")),
            "limit_up_time": None,
        })

    if rows:
        upsert_many("limit_up_records", rows)
        logger.info(f"炸板池写入 {len(rows)} 条")
    return True


def fetch_limit_down(trade_date: date = None) -> int:
    """
    采集跌停池，返回跌停数量（用于更新 market_sentiment）
    接口：stock_zt_pool_dtgc_em（新接口名）
    """
    if trade_date is None:
        trade_date = get_last_trading_date()
    date_str = trade_date.strftime("%Y%m%d")

    logger.info(f"采集跌停池: {date_str}")
    df = get_limit_down_pool(date_str)
    if df is None or df.empty:
        logger.warning("跌停池数据为空，返回0")
        return 0

    count = len(df)
    logger.info(f"跌停池: {count} 条")
    return count


def calc_and_store_sentiment(trade_date: date = None) -> bool:
    """
    计算并存储市场情绪：
    - 涨停/炸板数：从 limit_up_records 表统计
    - 上涨/下跌/涨停/跌停家数：从 stock_market_activity_legu 获取
    - 情绪分算法：涨停数 / (涨停数 + 炸板数) * 100
    """
    if trade_date is None:
        trade_date = get_last_trading_date()

    try:
        # 1. 从数据库统计涨停/炸板
        df = query("SELECT * FROM limit_up_records WHERE date = ?", [trade_date])
        limit_up = len(df[df["is_broken"] == False]) if not df.empty else 0
        broken = len(df[df["is_broken"] == True]) if not df.empty else 0

        # 2. 从乐估获取全市场上涨/下跌/涨停/跌停家数
        up_count = down_count = flat_count = limit_down_count = 0
        activity_df = get_market_activity()
        if activity_df is not None and not activity_df.empty:
            act = dict(zip(activity_df["item"], activity_df["value"]))
            up_count = int(float(str(act.get("上涨", 0)).replace("%", "") or 0))
            down_count = int(float(str(act.get("下跌", 0)).replace("%", "") or 0))
            flat_count = int(float(str(act.get("平盘", 0)).replace("%", "") or 0))
            # 真实跌停（不含ST）
            limit_down_count = int(float(str(act.get("真实跌停", 0)).replace("%", "") or 0))
            logger.info(f"市场活跃度: 上涨{up_count} 下跌{down_count} 平{flat_count} 跌停{limit_down_count}")

        # 3. 情绪分
        total = limit_up + broken
        score = round(limit_up / total * 100, 1) if total > 0 else 0.0

        upsert("market_sentiment", {
            "date": trade_date,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down_count,
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": flat_count,
            "total_volume": 0.0,   # 成交额由 moneyflow 模块填充
            "sentiment_score": score,
        }, pk=["date"])

        logger.info(f"情绪分: {score:.1f}，涨停{limit_up} 炸板{broken} 跌停{limit_down_count}")
        return True
    except Exception as e:
        logger.error(f"情绪分计算失败: {e}")
        return False


def run(trade_date: date = None):
    """完整采集流程"""
    fetch_limit_up(trade_date)
    fetch_limit_up_broken(trade_date)
    calc_and_store_sentiment(trade_date)


# 保留旧函数名兼容 scheduler
fetch_limit_down_broken = fetch_limit_up_broken


if __name__ == "__main__":
    from storage.duckdb_store import init_tables
    init_tables()
    run()
