"""
慧盘 · pkl 加载统一工具
utils/pkl_helper.py

消除各 collector 中重复的 pkl 加载/查询代码。
所有 collector 应通过此模块访问 pkl 数据,不再自行实现
_load_spot / _lookup_stock / _filter_valid 等函数。

设计原则:
- 行为与原 reversal_tracker 等价(迁移零语义变化)
- 失败返回 None,不抛异常(调用方便于处理)
- 字段名常量化,但允许混用字符串字面量
"""

import pickle
from pathlib import Path
from datetime import date
import pandas as pd
from loguru import logger

# ═══════════════════════════════════════════
# 路径常量
# ═══════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "static" / "data"
SPOT_CACHE = DATA_DIR / ".spot_cache.pkl"
ARCHIVE_DIR = DATA_DIR / "archive" / "spot"

# ═══════════════════════════════════════════
# 字段常量(AKShare stock_zh_a_spot_em 返回字段)
#
# 推荐调用方使用常量: row[FIELD_PCT]
# 允许字面量写法:     row["涨跌幅"]
# 两者等价,常量化便于未来字段变更统一修改
# ═══════════════════════════════════════════

FIELD_CODE = "代码"
FIELD_NAME = "名称"
FIELD_LAST = "最新价"
FIELD_CHG_AMT = "涨跌额"
FIELD_PCT = "涨跌幅"
FIELD_BID = "买入"
FIELD_ASK = "卖出"
FIELD_PREV_CLOSE = "昨收"
FIELD_OPEN = "今开"
FIELD_HIGH = "最高"
FIELD_LOW = "最低"
FIELD_VOL = "成交量"
FIELD_AMOUNT = "成交额"
FIELD_TIMESTAMP = "时间戳"


# ═══════════════════════════════════════════
# 核心函数
# ═══════════════════════════════════════════

def load_spot(path: Path = None) -> pd.DataFrame | None:
    """加载单个 pkl,返回 DataFrame 或 None

    pkl 结构: {"time": float, "df": DataFrame}
    兼容直接存 DataFrame 的旧格式。

    参数:
        path: pkl 文件路径,默认 SPOT_CACHE

    返回:
        DataFrame 或 None(文件不存在 / pkl 损坏 / df 为空)
    """
    if path is None:
        path = SPOT_CACHE
    if not path.exists():
        logger.debug(f"pkl 不存在: {path}")
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        df = data["df"] if isinstance(data, dict) and "df" in data else data
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.debug(f"pkl 数据为空: {path}")
            return None
        return df
    except Exception as e:
        logger.warning(f"pkl 加载失败 {path}: {e}")
        return None


def filter_valid(
    df: pd.DataFrame,
    exclude_bj: bool = True,
    exclude_st: bool = False,
    min_price: float = 0.0,
) -> pd.DataFrame:
    """过滤有效个股

    默认行为等价于原 reversal_tracker 的 _filter_valid:
    - 涨跌幅非空
    - 最新价 > 0
    - 排除北交所(bj 前缀)

    可选参数:
        exclude_st: 排除 ST/*ST 股票(名称含 "ST")
        min_price: 最低价格过滤(< min_price 的股被剔除)

    返回:
        新 DataFrame(不修改原 df)
    """
    mask = df[FIELD_PCT].notna() & (df[FIELD_LAST] > min_price)

    if exclude_bj:
        mask &= ~df[FIELD_CODE].str.startswith("bj")

    if exclude_st:
        mask &= ~df[FIELD_NAME].str.contains("ST", na=False, regex=False)

    return df[mask].copy()


def lookup_stock(df: pd.DataFrame, code: str) -> pd.Series | None:
    """按 6 位代码查股票,自动兼容 sh/sz 前缀

    参数:
        df: spot DataFrame
        code: 6 位纯数字代码(如 "600000"),不带前缀

    返回:
        Series 或 None(未找到)

    注: 北交所股票(bj 前缀)不会被匹配,符合 A 股反转分析需求。
    """
    row = df[
        (df[FIELD_CODE] == f"sh{code}") | (df[FIELD_CODE] == f"sz{code}")
    ]
    if row.empty:
        return None
    return row.iloc[0]


def find_archive_pkls(max_days: int = 10, exclude_today: bool = True) -> list[Path]:
    """获取最近 max_days 个归档 pkl 路径,按日期降序

    参数:
        max_days: 最多返回的 pkl 数量
        exclude_today: 是否排除今日归档(默认 True,用于历史对比)

    返回:
        Path 列表,按日期降序,长度 <= max_days
    """
    if not ARCHIVE_DIR.exists():
        return []

    today_str = date.today().strftime("%Y%m%d")
    pkls = sorted(ARCHIVE_DIR.glob("spot_*.pkl"), reverse=True)
    result = []

    for p in pkls:
        pkl_date = p.stem.replace("spot_", "")
        if exclude_today and pkl_date >= today_str:
            continue
        result.append(p)
        if len(result) >= max_days:
            break

    return result


def preload_archives(
    max_days: int = 10,
    exclude_today: bool = True,
) -> dict[str, pd.DataFrame]:
    """一次性预加载最近 N 天归档 pkl

    避免各 collector 重复读取磁盘(30 只股 x 10 个文件 = 300 次 -> 10 次)

    参数:
        max_days: 最多加载天数
        exclude_today: 是否排除今日

    返回:
        {YYYYMMDD: DataFrame},按日期顺序不保证
    """
    archive_pkls = find_archive_pkls(max_days, exclude_today)
    result = {}

    for p in archive_pkls:
        pkl_date = p.stem.replace("spot_", "")
        df = load_spot(p)
        if df is not None:
            result[pkl_date] = df

    logger.info(f"预加载归档 pkl: {len(result)}/{len(archive_pkls)} 个")
    return result


def pkl_date_to_iso(path: Path) -> str:
    """从 pkl 文件名提取日期

    spot_20260416.pkl -> 2026-04-16
    """
    d = path.stem.replace("spot_", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


# ═══════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    """独立运行测试: python -m utils.pkl_helper"""
    import sys

    # 独立测试时也用统一日志格式
    from utils.log_helper import setup_logger
    setup_logger()

    logger.info("=" * 50)
    logger.info("pkl_helper 独立测试")
    logger.info("=" * 50)

    # 测试 1: 加载 spot cache
    logger.info("测试 1: load_spot(默认 SPOT_CACHE)")
    df = load_spot()
    if df is None:
        logger.warning(f"  SPOT_CACHE 不存在或加载失败: {SPOT_CACHE}")
    else:
        logger.info(f"  加载成功: shape={df.shape}")
        logger.info(f"  字段: {list(df.columns)}")

    # 测试 2: 查找归档
    logger.info("测试 2: find_archive_pkls(max_days=10)")
    pkls = find_archive_pkls(10)
    logger.info(f"  找到 {len(pkls)} 个归档 pkl")
    for p in pkls[:3]:
        logger.info(f"    {p.name} -> {pkl_date_to_iso(p)}")

    # 测试 3: 预加载归档
    if pkls:
        logger.info("测试 3: preload_archives(max_days=3)")
        archives = preload_archives(max_days=3)
        logger.info(f"  预加载 {len(archives)} 个归档")

        # 测试 4: 单股查询
        if df is not None and archives:
            logger.info("测试 4: lookup_stock(平安银行 000001)")
            first_archive = list(archives.values())[0]
            row = lookup_stock(first_archive, "000001")
            if row is not None:
                logger.info(f"  找到: {row[FIELD_NAME]} 最新价={row[FIELD_LAST]} 涨跌幅={row[FIELD_PCT]}")
            else:
                logger.info("  未找到 000001")

    # 测试 5: filter_valid
    if df is not None:
        logger.info("测试 5: filter_valid")
        filtered = filter_valid(df)
        logger.info(f"  原始 {len(df)} -> 过滤后 {len(filtered)}(排除北交所等)")

    logger.info("=" * 50)
    logger.info("测试完成")
    sys.exit(0)
