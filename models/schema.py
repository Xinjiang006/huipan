"""
慧盘标准数据模型
约定：
- 股票代码：6位字符串，不加前缀（000001 而非 SH000001）
- 金额单位：亿元
- 涨跌幅单位：%（3.5 而非 0.035）
- 日期类型：Python date 对象
- 日期偏移：必须查 trade_calendar 表，不能用 timedelta
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class TradeCalendar:
    """交易日历"""
    date: date
    is_trading: bool
    prev_trading_date: Optional[date] = None
    next_trading_date: Optional[date] = None


@dataclass
class MarketSentiment:
    """大盘情绪（每日）"""
    date: date
    limit_up_count: int        # 涨停数
    limit_down_count: int      # 跌停数
    up_count: int              # 上涨数
    down_count: int            # 下跌数
    flat_count: int            # 平盘数
    total_volume: float        # 总成交额（亿元）
    sentiment_score: float     # 情绪分 0-100


@dataclass
class LimitUpRecord:
    """涨停记录（每日每股）"""
    date: date
    stock_code: str            # 6位股票代码
    stock_name: str
    consecutive_days: int      # 连板数
    is_broken: bool            # 是否炸板
    open_pct: float            # 竞价涨幅 %
    sector: str                # 所属板块
    limit_up_time: Optional[str] = None   # 首次涨停时间


@dataclass
class SectorFlow:
    """板块资金流向（每日每板块）"""
    date: date
    sector_name: str
    change_pct: float          # 涨跌幅 %
    main_net_inflow: float     # 主力净流入（亿元）
    total_volume: float        # 成交额（亿元）
    up_count: int              # 上涨股数
    down_count: int            # 下跌股数


@dataclass
class MoneyFlow:
    """资金流向（每日）"""
    date: date
    north_sh_net: float        # 北向沪股通净流入（亿元）
    north_sz_net: float        # 北向深股通净流入（亿元）
    north_total_net: float     # 北向合计净流入（亿元）
    main_net_inflow: float     # 主力净流入（亿元）
    retail_net_inflow: float   # 散户净流入（亿元）
    margin_balance: float      # 融资余额（亿元）


@dataclass
class ETFSnapshot:
    """ETF快照（每日每ETF）"""
    date: date
    fund_code: str             # ETF代码
    fund_name: str
    change_pct: float          # 涨跌幅 %
    share_change: float        # 份额变化（亿份），正为净申购
    total_share: float         # 总份额（亿份）
    total_size: float          # 总规模（亿元）
    category: str              # 类别（宽基/行业/债券等）


@dataclass
class NorthboundDaily:
    """北向资金每日详情"""
    date: date
    net_inflow: float          # 净流入（亿元）
    buy_amount: float          # 买入额（亿元）
    sell_amount: float         # 卖出额（亿元）
    consecutive_days: int      # 连续流入天数（负数=连续流出）
