"""
市场广度适配器 — 纯采集层，不做业务处理

接口清单：
  主：stock_rank_lxsz_ths  → 连续上涨股票列表
  主：stock_rank_lxxd_ths  → 连续下跌股票列表
  主：stock_zh_a_spot      → 全市场A股实时快照（新浪，限流风险低，每日1次）
  备：无                   → stock_rank_cxg/cxd_ths akshare内部bug，暂不可用

注意：
  - stock_zh_a_spot 偶发限流（返回HTML），已做异常捕获返回None
  - stock_rank_cxg_ths / stock_rank_cxd_ths 存在列数不匹配bug，暂时跳过，新高/新低填0
"""
from loguru import logger


def get_streak_up():
    """
    连续上涨股票列表 — 同花顺
    返回 DataFrame，列包含连涨天数
    """
    try:
        import akshare as ak
        df = ak.stock_rank_lxsz_ths()
        logger.debug(f"连续上涨: {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"连续上涨采集失败: {e}")
        return None


def get_streak_down():
    """
    连续下跌股票列表 — 同花顺
    返回 DataFrame，列包含连跌天数
    """
    try:
        import akshare as ak
        df = ak.stock_rank_lxxd_ths()
        logger.debug(f"连续下跌: {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"连续下跌采集失败: {e}")
        return None


def get_new_high():
    """
    创52周新高股票列表 — 同花顺
    ⚠️  当前 akshare 存在列数不匹配 bug（期望7列，实际8列）
    暂时返回 None，由上层填充 0，等待 akshare 修复后启用
    """
    # TODO: akshare stock_rank_cxg_ths 列名定义与实际不符，等待修复
    # try:
    #     import akshare as ak
    #     df = ak.stock_rank_cxg_ths()
    #     return df
    # except Exception as e:
    #     logger.error(f"创新高采集失败: {e}")
    #     return None
    logger.debug("创新高: akshare bug 暂时跳过，填0")
    return None


def get_new_low():
    """
    创52周新低股票列表 — 同花顺
    ⚠️  同上，akshare bug，暂时返回 None
    """
    # TODO: akshare stock_rank_cxd_ths 列名定义与实际不符，等待修复
    logger.debug("创新低: akshare bug 暂时跳过，填0")
    return None


def get_spot_snapshot():
    """
    全市场A股实时快照 — 新浪
    用于计算大中小微盘涨跌分布
    注意：偶发限流，调用前确保每天只调一次

    返回 DataFrame，核心列：代码、名称、最新价、涨跌幅、昨收、今开
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot()
        logger.debug(f"全市场快照: {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"全市场快照采集失败（可能限流）: {e}")
        return None
