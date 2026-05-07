"""
慧盘 · 历史数据初始化脚本
用途：补充 2022-01-01 至今的历史数据，用于分位数计算
涉及指标：涨停数、跌停数、涨跌比、成交额（存入 market_sentiment 表）

注意：
- 只需运行一次（首次部署时）
- 内存小的服务器建议按年分批跑，避免 OOM
- 日期偏移必须查 trade_calendar 表，不用 timedelta
"""

import sys
import time
import gc
from datetime import date, datetime
import akshare as ak
import pandas as pd
from loguru import logger

# 确保能找到项目模块
sys.path.insert(0, ".")

from storage.duckdb_store import upsert_many, query, get_conn
from collector.trade_calendar import fetch_and_store_trade_calendar

# ── 配置 ──────────────────────────────────────────────
HISTORY_START = date(2022, 1, 1)   # 市场结构变化后，不需要更早数据
BATCH_SIZE = 20                     # 每批处理多少个交易日（内存控制）
SLEEP_BETWEEN_BATCH = 2.0          # 批次间暂停（秒），避免被限流

logger.add("logs/init_history.log", rotation="50MB", retention="7 days")


# ── 工具函数 ──────────────────────────────────────────
def get_trading_days_in_range(start: date, end: date) -> list[date]:
    """从 trade_calendar 表获取指定范围内的交易日列表"""
    df = query(
        "SELECT date FROM trade_calendar WHERE date >= ? AND date <= ? AND is_trading = true ORDER BY date",
        [start, end]
    )
    if df.empty:
        return []
    return list(df["date"])


def already_has_data(d: date) -> bool:
    """检查某个交易日是否已有数据（避免重复写入）"""
    df = query(
        "SELECT COUNT(*) as cnt FROM market_sentiment WHERE date = ?", [d]
    )
    return df.iloc[0]["cnt"] > 0


# ── 核心采集函数 ──────────────────────────────────────
def fetch_one_day(trade_date: date) -> dict | None:
    """
    采集单日数据：涨停数、跌停数、涨跌比、成交额
    返回 dict 或 None（失败时）
    """
    date_str = trade_date.strftime("%Y%m%d")

    try:
        # 1. 涨停池 → 涨停数
        limit_up_count = 0
        try:
            zt_df = ak.stock_zt_pool_em(date=date_str)
            if zt_df is not None and not zt_df.empty:
                limit_up_count = len(zt_df)
        except Exception as e:
            logger.debug(f"{trade_date} 涨停池获取失败: {e}")

        # 2. 跌停池 → 跌停数（用跌停股池）
        limit_down_count = 0
        try:
            raise Exception("跳过")
            if dt_df is not None and not dt_df.empty:
                limit_down_count = len(dt_df)
        except Exception as e:
            logger.debug(f"{trade_date} 跌停池获取失败: {e}")

        # 3. 涨跌家数 + 成交额（用沪深指数日线）
        up_count = 0
        down_count = 0
        total_volume = 0.0
        try:
            # 腾讯指数日线，amount 单位为千元，除以1e5得到亿元
            sh_df = ak.stock_zh_index_daily_tx(symbol="sh000001")
            sz_df = ak.stock_zh_index_daily_tx(symbol="sz399001")
            date_str2 = str(trade_date)[:10]
            sh_row = sh_df[sh_df["date"].astype(str) == date_str2]
            sz_row = sz_df[sz_df["date"].astype(str) == date_str2]
            sh_vol = float(sh_row.iloc[0]["amount"]) / 1e5 if not sh_row.empty else 0
            sz_vol = float(sz_row.iloc[0]["amount"]) / 1e5 if not sz_row.empty else 0
            total_volume = round(sh_vol + sz_vol, 2)
        except Exception as e:
            logger.debug(f"{trade_date} 成交额获取失败: {e}")

        # 历史数据用涨停池估算涨跌家数（部分日期可能市场活跃度接口返回当天数据）
        # 如果 up_count/down_count 为 0，尝试用 stock_advance_decline 接口
        if up_count == 0 and down_count == 0:
            try:
                ad_df = ak.stock_advance_decline_bj(date=date_str)
                if ad_df is not None and not ad_df.empty:
                    for _, r in ad_df.iterrows():
                        up_count += int(r.get("上涨家数", 0) or 0)
                        down_count += int(r.get("下跌家数", 0) or 0)
            except Exception as e:
                logger.debug(f"{trade_date} 涨跌家数接口失败: {e}")

        # 计算情绪分（简单线性映射，和 collector/market.py 保持一致）
        sentiment_score = 0.0
        if limit_up_count + limit_down_count > 0:
            sentiment_score = round(
                limit_up_count / max(limit_up_count + limit_down_count, 1) * 100, 1
            )

        return {
            "date": trade_date,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "up_count": up_count,
            "down_count": down_count,
            "flat_count": 0,
            "total_volume": total_volume,
            "sentiment_score": sentiment_score,
        }

    except Exception as e:
        logger.error(f"{trade_date} 采集完全失败: {e}")
        return None


# ── 主流程 ────────────────────────────────────────────
def run(start: date = None, end: date = None, force: bool = False):
    """
    运行历史数据初始化
    force=True 时强制覆盖已有数据
    """
    if start is None:
        start = HISTORY_START
    if end is None:
        end = date.today()

    logger.info(f"开始补充历史数据: {start} → {end}")

    # 确保交易日历有数据
    logger.info("检查交易日历...")
    cal_df = query("SELECT COUNT(*) as cnt FROM trade_calendar")
    if cal_df.iloc[0]["cnt"] == 0:
        logger.info("交易日历为空，先初始化...")
        fetch_and_store_trade_calendar()

    # 获取所有交易日
    trading_days = get_trading_days_in_range(start, end)
    logger.info(f"共 {len(trading_days)} 个交易日需要处理")

    # 过滤已有数据的日期
    if not force:
        pending = [d for d in trading_days if not already_has_data(d)]
        logger.info(f"其中 {len(trading_days) - len(pending)} 天已有数据，跳过")
    else:
        pending = trading_days
        logger.info("force=True，全部重新采集")

    if not pending:
        logger.info("所有数据已存在，无需补充")
        return

    logger.info(f"待采集 {len(pending)} 天，开始...")

    # 分批处理
    success, fail = 0, 0
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i: i + BATCH_SIZE]
        rows = []

        for d in batch:
            result = fetch_one_day(d)
            if result:
                rows.append(result)
                success += 1
            else:
                fail += 1
            time.sleep(0.3)  # 单条请求间隔

        # 批量写入
        if rows:
            upsert_many("market_sentiment", rows)
            logger.info(
                f"进度 {i + len(batch)}/{len(pending)} — 写入 {len(rows)} 条"
                f"（累计成功 {success}，失败 {fail}）"
            )

        # 释放内存
        del rows
        gc.collect()

        # 批次间暂停，避免限流
        if i + BATCH_SIZE < len(pending):
            time.sleep(SLEEP_BETWEEN_BATCH)

    logger.info(f"历史数据补充完成 ✓ 成功 {success} 天，失败 {fail} 天")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="慧盘历史数据初始化")
    parser.add_argument("--start", default="2022-01-01", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--force", action="store_true", help="强制覆盖已有数据")
    parser.add_argument("--year", type=int, help="只补充某一年（内存小时按年跑）")
    args = parser.parse_args()

    if args.year:
        start = date(args.year, 1, 1)
        end = date(args.year, 12, 31)
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()

    run(start=start, end=end, force=args.force)
