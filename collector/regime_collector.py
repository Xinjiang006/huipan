"""
慧盘 · Regime 日度计算模块（编排层）
v5.2 — T+1数据源改为picks_history（方案B，解决16:45覆盖问题）

本模块只做编排：调 IO 加载数据 → 调 compute 计算指标 → 调 IO 写入。
不含任何计算逻辑或文件路径硬编码。

可独立运行：python3 collector/regime_collector.py
可被 import：from collector.regime_collector import collect_regime
"""

import logging
import time
from datetime import datetime, date

from utils.stock_filter import calc_limit_counts, calc_basic_counts
from compute.indicators import (
    build_today_chg_map,
    calc_next_day_returns,
    calc_next_day_returns_by_tier,
    calc_cap_dist,
    calc_price_dist,
    calc_sector_stats,
    calc_derived_indicators,
    calc_health_indicators,
    apply_regime_label,
    TIERS,
)
from compute.prior_analysis import calc_prior_analysis
from data_io.regime_io import (
    load_spot_cache,
    check_pkl_freshness,
    load_yesterday_picks,
    load_picks_from_history,
    backfill_picks_history_t1,
    find_yesterday_limit_up_codes,
    load_supplementary_data,
    query_history_for_health,
    load_prior_close_maps,
    save_to_duckdb,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)


def collect_regime():
    """主入口 — v5.0 计算与 IO 分离版"""
    t_start = time.time()
    log.info("═══ regime_collector 开始计算 ═══")
    today_date = datetime.now().strftime("%Y-%m-%d")
    log.info(f"collect_regime: today_date={today_date}")

    # ── 0. 非交易日保护 ──
    try:
        from collector.trade_calendar import is_trading_day
        trading = is_trading_day(date.today())
        if not trading:
            log.info(f"collect_regime: {today_date} 非交易日, 跳过")
            return None
        log.debug(f"collect_regime: {today_date} 是交易日")
    except ImportError:
        log.warning("collect_regime: trade_calendar不可用, 跳过交易日校验")

    # ── 1. 加载今日全市场行情 ──
    df = load_spot_cache()
    if df is None:
        log.error("collect_regime: 无行情缓存, 退出")
        return None

    # pkl 新鲜度校验
    pkl_age = check_pkl_freshness()
    if pkl_age is not None and pkl_age > 6:
        log.warning(f"collect_regime: pkl已过期 ({pkl_age:.1f}h前), 可能非当日数据, 跳过")
        return None
    if pkl_age is not None:
        log.info(f"collect_regime: pkl新鲜度 {pkl_age:.1f}h前")

    # ── 2. 从今日 spot 提取涨跌 Top100 ──
    from collector.ashare_movers import build_picks_from_df, load_sector_map, load_index_cons_map
    sector_map = load_sector_map()
    cons_map = load_index_cons_map()
    today_gainers, today_losers = build_picks_from_df(df, sector_map, cons_map)
    if today_gainers is None:
        log.error("collect_regime: 无法从spot提取Top100, 退出")
        return None

    log.info(f"collect_regime: 今日spot提取 涨Top{len(today_gainers)}/跌Top{len(today_losers)}")

    # ── 3. 当日分布（纯计算）──
    gainer_cap   = calc_cap_dist(today_gainers)
    loser_cap    = calc_cap_dist(today_losers)
    gainer_price = calc_price_dist(today_gainers)
    loser_price  = calc_price_dist(today_losers)
    sector_stats = calc_sector_stats(today_gainers, today_losers)

    log.info(f"collect_regime: 涨幅市值 微{gainer_cap['微盘']}/小{gainer_cap['小盘']}/中{gainer_cap['中盘']}/大{gainer_cap['大盘']}")
    log.info(f"collect_regime: 板块 涨{sector_stats['sector_count_gainers']}个 重合{sector_stats['sector_overlap']}个 微盘热度{sector_stats['micro_cap_ratio_gainer']}%")

    # ── 4. T+1 次日收益（方案B: 从 picks_history 读取）──
    log.info("── T+1 次日收益计算 ──")
    picks = load_picks_from_history(today_date)
    picks_date = None
    mom_returns = {"avg": None, "median": None, "up_count": 0, "matched": 0}
    rev_returns = {"avg": None, "median": None, "up_count": 0, "matched": 0}
    mom_tier = {t: {"avg": None, "median": None, "up_count": 0, "n": 0} for t in TIERS}
    rev_tier = {t: {"avg": None, "median": None, "up_count": 0, "n": 0} for t in TIERS}

    if picks is not None:
        gainers = picks.get("top100_gainers", [])
        losers  = picks.get("top100_losers", [])
        picks_date = picks.get("date")

        log.info(f"collect_regime: picks_date={picks_date}, today_date={today_date}")
        if picks_date == today_date:
            log.warning(f"collect_regime: ⚠️ 自比检测! picks_date({picks_date}) == today_date({today_date}), T+1将无意义")

        chg_map = build_today_chg_map(df)
        log.debug(f"collect_regime: chg_map 构建完成, {len(chg_map)} 只")

        mom_returns = calc_next_day_returns(gainers, chg_map)
        rev_returns = calc_next_day_returns(losers, chg_map)
        log.info(f"collect_regime: 追涨T+1 avg={mom_returns['avg']}% 上涨{mom_returns['up_count']}/{mom_returns['matched']}")
        log.info(f"collect_regime: 抄底T+1 avg={rev_returns['avg']}% 上涨{rev_returns['up_count']}/{rev_returns['matched']}")

        mom_tier = calc_next_day_returns_by_tier(gainers, chg_map)
        rev_tier = calc_next_day_returns_by_tier(losers, chg_map)
        for t in TIERS:
            mt, rt = mom_tier[t], rev_tier[t]
            log.debug(f"  {t}: 追涨avg={mt['avg']}%({mt['n']}只) 抄底avg={rt['avg']}%({rt['n']}只)")
    else:
        log.info("collect_regime: 无picks历史数据, T+1收益为null")

    # ── 5. 补充热度+趋势 (IO) ──
    sup = load_supplementary_data(df=df)
    if sup.get("up_count") is None:
        basics = calc_basic_counts(df)
        sup["up_count"] = basics["up_count"]
        sup["down_count"] = basics["down_count"]
        sup["up_ratio"] = basics["up_ratio"]
        log.debug(f"collect_regime: up/down从stock_filter计算 up={sup['up_count']} down={sup['down_count']} ratio={sup['up_ratio']}")

    log.debug(f"collect_regime: sup keys={list(sup.keys())}")

    # ── 6. 衍生指标（IO 提供昨日涨停股 → 纯计算）──
    yd_codes = find_yesterday_limit_up_codes()
    derived = calc_derived_indicators(df, today_gainers, today_losers, sup, yd_limit_up_codes=yd_codes)
    log.debug(f"collect_regime: 衍生指标 keys={list(derived.keys())}")

    # ── 7. 健康指标（v5.3: 日内强度+VWAP偏离）──
    hist = query_history_for_health(today_date)
    health = calc_health_indicators(df, sup, history_data=hist)
    log.debug(f"collect_regime: 健康指标 keys={list(health.keys())}")

    # ── 8. 前置分析（IO 加载归档 → 纯计算）──
    chg_t1, close_t1, close_t4, close_t6 = load_prior_close_maps(today_date)
    prior = calc_prior_analysis(df, chg_t1, close_t1, close_t4, close_t6)
    log.debug(f"collect_regime: 前置分析 keys={list(prior.keys())}")

    # ── 9. 组装 record ──
    record = {
        "date": today_date,
        "return_date": today_date,
        "picks_date": picks_date,

        "volume_total":    sup.get("volume_total"),
        "volume_rank_30d": sup.get("volume_rank_30d"),
        "limit_up":        sup.get("limit_up"),
        "limit_down":      sup.get("limit_down"),
        "up_count":        sup.get("up_count"),
        "down_count":      sup.get("down_count"),
        "up_ratio":        sup.get("up_ratio"),

        "sh_change_pct":      sup.get("sh_change_pct"),
        "sz_change_pct":      sup.get("sz_change_pct"),
        "cyb_change_pct":     sup.get("cyb_change_pct"),
        "csi1000_change_pct": sup.get("csi1000_change_pct"),

        "momentum_avg_return":    mom_returns["avg"],
        "momentum_median_return": mom_returns["median"],
        "momentum_up_count":      mom_returns["up_count"],
        "momentum_matched":       mom_returns["matched"],
        "reversion_avg_return":   rev_returns["avg"],
        "reversion_median_return":rev_returns["median"],
        "reversion_up_count":     rev_returns["up_count"],
        "reversion_matched":      rev_returns["matched"],

        "gainer_micro": gainer_cap.get("微盘", 0),
        "gainer_small": gainer_cap.get("小盘", 0),
        "gainer_mid":   gainer_cap.get("中盘", 0),
        "gainer_large": gainer_cap.get("大盘", 0),
        "loser_micro":  loser_cap.get("微盘", 0),
        "loser_small":  loser_cap.get("小盘", 0),
        "loser_mid":    loser_cap.get("中盘", 0),
        "loser_large":  loser_cap.get("大盘", 0),

        "gainer_p0_10":   gainer_price.get("0-10", 0),
        "gainer_p10_30":  gainer_price.get("10-30", 0),
        "gainer_p30_50":  gainer_price.get("30-50", 0),
        "gainer_p50_100": gainer_price.get("50-100", 0),
        "gainer_p100p":   gainer_price.get("100+", 0),
        "loser_p0_10":    loser_price.get("0-10", 0),
        "loser_p10_30":   loser_price.get("10-30", 0),
        "loser_p30_50":   loser_price.get("30-50", 0),
        "loser_p50_100":  loser_price.get("50-100", 0),
        "loser_p100p":    loser_price.get("100+", 0),

        **sector_stats,
        **derived,
        **health,
        **prior,
    }

    # 分档 T+1
    for prefix, tier_data in [("mom", mom_tier), ("rev", rev_tier)]:
        for tier in TIERS:
            td = tier_data[tier]
            record[f"{prefix}_{tier}_avg"]    = td["avg"]
            record[f"{prefix}_{tier}_median"] = td["median"]
            record[f"{prefix}_{tier}_up"]     = td["up_count"]
            record[f"{prefix}_{tier}_n"]      = td["n"]

    log.info(f"collect_regime: record组装完成, {len(record)} 个字段")
    log.info(f"collect_regime: 关键字段 → date={record['date']}, picks_date={record['picks_date']}, return_date={record['return_date']}")
    log.debug(f"collect_regime: T+1 → mom_avg={record['momentum_avg_return']}, rev_avg={record['reversion_avg_return']}, matched={record['momentum_matched']}")

    # ── 9.5 回填 picks_history T+1（方案B补全）──
    if picks is not None and picks_date and mom_returns["avg"] is not None:
        log.info(f"collect_regime: 触发 T+1 回填 (picks_date={picks_date}, mom_avg={mom_returns['avg']})")
        backfill_picks_history_t1(picks_date, mom_returns, rev_returns)
    else:
        log.debug(f"collect_regime: T+1回填跳过 (picks={'有' if picks else '无'}, picks_date={picks_date}, mom_avg={mom_returns['avg']})")

    # ── 10. 打标签 ──
    record["regime_label"] = apply_regime_label(record)
    log.info(f"collect_regime: regime_label={record['regime_label']} (date={today_date}, picks={picks_date})")

    # ── 11. 写 DuckDB + 导出 JSON ──
    save_to_duckdb(record)

    elapsed = time.time() - t_start
    log.info(f"═══ regime_collector 完成 ({elapsed:.1f}s) ═══")

    return record


if __name__ == "__main__":
    collect_regime()
