"""
大宗交易采集器
- stock_dzjy_sctj() → 市场统计（折价成交总额占比等）

调度：每日 16:30（大宗交易数据更新较晚）
"""
from datetime import date
from typing import Optional
from loguru import logger


def _safe_float(val, default=0.0) -> float:
    try:
        if val is None or str(val).strip() in ("", "-", "—", "nan"):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def fetch_block_trade_stats() -> list:
    """获取大宗交易市场统计（全部历史），返回最近 N 天数据"""
    try:
        import akshare as ak
        df = ak.stock_dzjy_sctj()
        if df is None or df.empty:
            logger.warning("大宗交易统计返回空")
            return []

        # 列名：序号、交易日期、上证指数、上证指数涨跌幅、大宗交易成交总额、
        #       溢价成交总额、溢价成交总额占比、折价成交总额、折价成交总额占比
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "date": str(r.get("交易日期", "")),
                # 占比字段单位已经是 %（如 88.6），直接存，不要再做任何换算
                "discount_ratio": _safe_float(r.get("折价成交总额占比")),
                "premium_ratio":  _safe_float(r.get("溢价成交总额占比")),
                "discount_amount": _safe_float(r.get("折价成交总额")),
                "premium_amount":  _safe_float(r.get("溢价成交总额")),
                "total_amount":    _safe_float(r.get("大宗交易成交总额")),
            })

        logger.info(f"大宗交易统计: {len(rows)} 条")
        return rows
    except Exception as e:
        logger.error(f"大宗交易统计采集失败: {e}")
        return []


def calc_latest_with_rank(rows: list, window: int = 30) -> dict:
    """
    从历史数据取最新一条 + 30日排名
    discount_ratio 单位是 %（如 88.6），直接展示
    """
    if not rows:
        return {"discount_ratio": 0, "rank": "—"}

    sorted_rows = sorted(rows, key=lambda x: x["date"], reverse=True)
    latest = sorted_rows[0]
    recent = sorted_rows[:window]

    if len(recent) < 2:
        rank_str = "—"
    else:
        val = latest["discount_ratio"]
        # 折价率越高 = 出货压力越大，排名从低到高
        higher = sum(1 for r in recent if r["discount_ratio"] <= val)
        rank_str = f"{higher} / {len(recent)}"

    return {
        "date": latest["date"],
        "discount_ratio": round(latest["discount_ratio"], 2),   # 单位 %，如 88.62
        "premium_ratio":  round(latest["premium_ratio"], 2),
        "total_amount":   latest["total_amount"],
        "rank": rank_str,
    }


def run(trade_date: Optional[date] = None) -> dict:
    """采集大宗交易统计并计算排名"""
    logger.info("开始采集大宗交易数据...")
    rows = fetch_block_trade_stats()
    result = calc_latest_with_rank(rows)
    # 直接用数值展示，不用 :.1% 格式化（会再乘100）
    logger.info(f"大宗折价率: {result['discount_ratio']:.1f}%, 排名 {result['rank']}")
    return result
