"""
市场广度采集器
业务逻辑层：调用 adapters/breadth_data.py，聚合计算，返回结构化数据

输出字段：
  - up_count / down_count / flat_count   上涨/下跌/平盘家数（从 market_sentiment 读，避免重复调新浪接口）
  - streak_up_3d / streak_down_3d        连涨/连跌3日以上
  - new_high_count / new_low_count       创新高/新低（暂填0，等akshare修复）
  - cap_tiers                            大中小微盘涨跌分布（新浪接口无市值列，暂为空）

调度：每日 15:50（收盘后数据稳定，且 market_sentiment 已写入）
"""
from datetime import date
from typing import Optional
from loguru import logger
from collector.adapters.breadth_data import (
    get_streak_up,
    get_streak_down,
    get_new_high,
    get_new_low,
)


def _get_up_down_from_db(trade_date: date) -> dict:
    """
    从 market_sentiment 表读取上涨/下跌/平盘家数
    避免重复调新浪接口导致限流
    """
    empty = {"up_count": 0, "down_count": 0, "flat_count": 0}
    try:
        from storage.duckdb_store import query
        df = query(
            "SELECT up_count, down_count, flat_count FROM market_sentiment WHERE date = ?",
            [trade_date]
        )
        if df.empty:
            logger.warning(f"market_sentiment 无 {trade_date} 数据，up/down count 填0")
            return empty
        row = df.iloc[0]
        return {
            "up_count": int(row.get("up_count", 0) or 0),
            "down_count": int(row.get("down_count", 0) or 0),
            "flat_count": int(row.get("flat_count", 0) or 0),
        }
    except Exception as e:
        logger.error(f"从数据库读取上涨/下跌家数失败: {e}")
        return empty


def calc_streak_counts(lxsz_df, lxxd_df, min_days: int = 3) -> dict:
    """从同花顺连续涨跌数据计算 >= min_days 天的家数"""
    up_3 = down_3 = 0

    if lxsz_df is not None and not lxsz_df.empty:
        day_col = next((c for c in lxsz_df.columns if "天数" in str(c) or "连涨" in str(c)), None)
        up_3 = int((lxsz_df[day_col] >= min_days).sum()) if day_col else len(lxsz_df)

    if lxxd_df is not None and not lxxd_df.empty:
        day_col = next((c for c in lxxd_df.columns if "天数" in str(c) or "连跌" in str(c)), None)
        down_3 = int((lxxd_df[day_col] >= min_days).sum()) if day_col else len(lxxd_df)

    return {"streak_up_3d": up_3, "streak_down_3d": down_3}


def calc_new_high_low(cxg_df, cxd_df) -> dict:
    """创新高/新低家数（akshare bug期间填0）"""
    return {
        "new_high_count": len(cxg_df) if cxg_df is not None and not cxg_df.empty else 0,
        "new_low_count": len(cxd_df) if cxd_df is not None and not cxd_df.empty else 0,
    }


def run(trade_date: Optional[date] = None) -> dict:
    """执行全部市场广度采集，返回聚合结果"""
    if trade_date is None:
        trade_date = date.today()

    logger.info("开始采集市场广度数据...")

    # 上涨/下跌家数：从数据库读，不重复调新浪接口
    up_down = _get_up_down_from_db(trade_date)

    lxsz_df = get_streak_up()
    lxxd_df = get_streak_down()
    cxg_df  = get_new_high()
    cxd_df  = get_new_low()

    result = {
        "date": str(trade_date),
        **up_down,
        **calc_streak_counts(lxsz_df, lxxd_df),
        **calc_new_high_low(cxg_df, cxd_df),
        "cap_tiers": [],
    }

    logger.info(
        f"市场广度: 上涨{result['up_count']} 下跌{result['down_count']} "
        f"连涨3日{result['streak_up_3d']} 连跌3日{result['streak_down_3d']} "
        f"新高{result['new_high_count']} 新低{result['new_low_count']}"
    )
    return result
