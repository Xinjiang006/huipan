"""
交易日历采集器
- 接口：ak.tool_trade_date_hist_sina()
- 最基础依赖，其他所有模块依赖此表
- 注意：文件名不能用 calendar.py（与Python内置模块冲突）
"""
import akshare as ak
import pandas as pd
from datetime import date, datetime
from loguru import logger
from storage.duckdb_store import upsert_many, query


def fetch_and_store_trade_calendar():
    """拉取全量交易日历并存储"""
    logger.info("开始拉取交易日历...")
    try:
        df = ak.tool_trade_date_hist_sina()
        # 返回列：trade_date
        trading_dates = set(pd.to_datetime(df["trade_date"]).dt.date)

        # 生成从 2010-01-01 到今天的日历
        from datetime import timedelta
        start = date(2010, 1, 1)
        end = date.today() + timedelta(days=30)
        all_dates = []
        d = start
        while d <= end:
            all_dates.append(d)
            d += timedelta(days=1)

        trading_list = sorted(trading_dates)
        rows = []
        for d in all_dates:
            is_trading = d in trading_dates
            # 找上一个交易日
            prev = None
            for td in reversed(trading_list):
                if td < d:
                    prev = td
                    break
            # 找下一个交易日
            nxt = None
            for td in trading_list:
                if td > d:
                    nxt = td
                    break
            rows.append({
                "date": d,
                "is_trading": is_trading,
                "prev_trading_date": prev,
                "next_trading_date": nxt,
            })

        upsert_many("trade_calendar", rows)
        logger.info(f"交易日历写入完成，共 {len(rows)} 条")
        return True
    except Exception as e:
        logger.error(f"交易日历采集失败: {e}")
        return False


def is_trading_day(d: date = None) -> bool:
    """判断某天是否为交易日"""
    if d is None:
        d = date.today()
    result = query(
        "SELECT is_trading FROM trade_calendar WHERE date = ?",
        [d]
    )
    if result.empty:
        is_weekday = d.weekday() < 5
        if is_weekday:
            print(f"  ⚠️ trade_calendar缺少{d}，工作日默认放行")
        return is_weekday
    return bool(result.iloc[0]["is_trading"])


def get_prev_trading_date(d: date = None) -> date:
    """获取上一个交易日"""
    if d is None:
        d = date.today()
    result = query(
        "SELECT prev_trading_date FROM trade_calendar WHERE date = ?",
        [d]
    )
    if result.empty:
        return None
    return result.iloc[0]["prev_trading_date"]


def get_last_trading_date() -> date:
    """获取最近一个交易日（今天或往前找）"""
    d = date.today()
    for _ in range(10):
        if is_trading_day(d):
            return d
        d = get_prev_trading_date(d)
        if d is None:
            break
    return None


if __name__ == "__main__":
    from storage.duckdb_store import init_tables
    init_tables()
    fetch_and_store_trade_calendar()
    print("今天是否交易日:", is_trading_day())
    print("上一个交易日:", get_prev_trading_date())
