"""
市场核心数据适配器
涨停池 / 跌停池 / 炸板池 / 市场活跃度 / 板块资金流向
"""
import akshare as ak
from loguru import logger


def get_limit_up(date_str: str):
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        logger.info(f"涨停池 {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"涨停池采集失败: {e}")
        return None


def get_limit_down(date_str: str):
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date_str)
        logger.info(f"跌停池 {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"跌停池采集失败: {e}")
        return None


def get_zhaban(date_str: str):
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
        logger.info(f"炸板池 {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"炸板池采集失败: {e}")
        return None


def get_market_activity():
    try:
        df = ak.stock_market_activity_legu()
        logger.info(f"市场活跃度 {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"市场活跃度采集失败: {e}")
        return None


def get_sector_flow():
    """
    板块资金流向
    主：东财 stock_sector_fund_flow_rank（push2.eastmoney.com，容易被封）
    备：同花顺 stock_fund_flow_industry（90条，字段略有不同）
    """
    # 主接口
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日")
        logger.info(f"板块资金(东财) {len(df)} 条")
        return df, "eastmoney"
    except Exception as e:
        logger.warning(f"板块资金东财失败，切换同花顺: {e}")

    # 备用接口
    try:
        df = ak.stock_fund_flow_industry(symbol="即时")
        if df is not None and not df.empty:
            # 字段标准化，对齐东财格式
            df = df.rename(columns={
                "行业":       "name",
                "行业-涨跌幅": "change_pct",
                "净额":       "net_flow",
                "流入资金":    "inflow",
                "流出资金":    "outflow",
                "公司家数":    "stock_count",
                "行业指数":    "index_val",
            })
            # 近似成交额 = 流入 + 流出
            df["amount"] = df["inflow"] + df["outflow"]
            logger.info(f"板块资金(同花顺备用) {len(df)} 条")
            return df, "ths"
    except Exception as e2:
        logger.error(f"板块资金同花顺也失败: {e2}")

    return None, None
