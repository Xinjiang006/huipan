"""
北向资金详情采集器
接口：ak.stock_hsgt_fund_flow_summary_em()（实时汇总，当日数据）
同时写入 northbound_daily 和 money_flow 两张表
"""
import akshare as ak
from datetime import date
from loguru import logger
from storage.duckdb_store import upsert, upsert_many, query
from collector.trade_calendar import get_last_trading_date


def fetch_northbound_detail(trade_date: date = None) -> bool:
    if trade_date is None:
        trade_date = get_last_trading_date()
    logger.info(f"采集北向资金: {trade_date}")
    try:
        df = ak.stock_hsgt_fund_flow_summary_em()
        north = df[df["资金方向"] == "北向"]

        sh_row = north[north["板块"] == "沪股通"]
        sz_row = north[north["板块"] == "深股通"]

        # 交易状态=3 表示非交易日，跳过
        if not sh_row.empty and str(sh_row.iloc[0].get("交易状态", "")) == "3":
            logger.info("北向：非交易日(状态3)，跳过写入")
            return False
        sh_net = float(sh_row.iloc[0]["成交净买额"]) if not sh_row.empty else 0.0
        sz_net = float(sz_row.iloc[0]["成交净买额"]) if not sz_row.empty else 0.0
        total_net = round(sh_net + sz_net, 4)

        # 计算连续流入/流出天数
        consecutive = _calc_consecutive(trade_date, total_net)

        # 写 northbound_daily
        upsert("northbound_daily", {
            "date":             trade_date,
            "net_inflow":       total_net,
            "buy_amount":       0.0,
            "sell_amount":      0.0,
            "consecutive_days": consecutive,
        }, pk=["date"])

        # 同步更新 money_flow
        upsert("money_flow", {
            "date":              trade_date,
            "north_sh_net":      sh_net,
            "north_sz_net":      sz_net,
            "north_total_net":   total_net,
            "main_net_inflow":   0.0,
            "retail_net_inflow": 0.0,
            "margin_balance":    0.0,
        }, pk=["date"])

        direction = "流入" if total_net >= 0 else "流出"
        logger.info(f"北向: 沪{sh_net:.1f} 深{sz_net:.1f} 合计{total_net:.1f}亿 连续{direction}{abs(consecutive)}天")
        return True
    except Exception as e:
        logger.error(f"北向资金采集失败: {e}")
        return False


def _calc_consecutive(trade_date: date, today_net: float) -> int:
    """从 northbound_daily 历史记录计算连续流入/流出天数"""
    direction = 1 if today_net >= 0 else -1
    try:
        df = query(
            "SELECT date, net_inflow FROM northbound_daily WHERE date < ? ORDER BY date DESC LIMIT 60",
            [trade_date]
        )
        count = 1
        for _, row in df.iterrows():
            net = float(row.get("net_inflow", 0) or 0)
            if (1 if net >= 0 else -1) == direction:
                count += 1
            else:
                break
        return count * direction
    except Exception:
        return direction


def run(trade_date: date = None):
    fetch_northbound_detail(trade_date)
