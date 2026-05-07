"""
慧盘 · IO 层（文件读写 + DuckDB 存储 + JSON 导出）
v5.0 — 从 regime_collector.py 剥离

职责：读 pkl/JSON，写 DuckDB，导出 regime_history.json。
     不做任何指标计算。
"""

import json
import os
import math
import time
import logging
import pickle
from datetime import datetime

from utils.stock_filter import find_limit_up_codes
from compute.indicators import TIERS, build_today_chg_map
from compute.prior_analysis import build_close_map

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
CACHE_PATH = os.path.join(DATA_DIR, ".spot_cache.pkl")
PICKS_PATH = os.path.join(DATA_DIR, "yesterday_picks.json")
HISTORY_PATH = os.path.join(DATA_DIR, "regime_history.json")
DB_PATH = os.path.join(BASE_DIR, "data", "huipan.duckdb")
ARCHIVE_SPOT_DIR = os.path.join(DATA_DIR, "archive", "spot")


# ══════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════

def load_spot_cache(max_retry=3, retry_interval=5):
    """读取全市场行情缓存（带重试），返回 DataFrame 或 None"""
    log.info("load_spot_cache: 开始加载")
    for attempt in range(1, max_retry + 1):
        try:
            with open(CACHE_PATH, "rb") as f:
                cache = pickle.load(f)
            df = cache.get("df")
            if df is not None and len(df) > 0:
                age = time.time() - cache.get("time", 0)
                log.info(f"load_spot_cache: 加载成功 ({len(df)}只, {age:.0f}s前)")
                return df
            raise ValueError("pkl内容为空")
        except Exception as e:
            log.warning(f"load_spot_cache: 第{attempt}次失败: {e}")
            if attempt < max_retry:
                time.sleep(retry_interval)

    log.error(f"load_spot_cache: 读取失败 (已重试{max_retry}次)")
    return None


def check_pkl_freshness():
    """检查 pkl 新鲜度，返回小时数。超过 6h 视为过期。"""
    try:
        with open(CACHE_PATH, "rb") as f:
            cache_meta = pickle.load(f)
        hours = (time.time() - cache_meta.get("time", 0)) / 3600
        log.debug(f"check_pkl_freshness: {hours:.1f}h前")
        return hours
    except Exception as e:
        log.debug(f"check_pkl_freshness: 检查失败: {e}")
        return None


def load_yesterday_picks():
    """读取 T 日 Top100 涨跌榜，返回 dict 或 None"""
    log.debug("load_yesterday_picks: 开始")
    if not os.path.exists(PICKS_PATH):
        log.warning("load_yesterday_picks: yesterday_picks.json不存在")
        return None

    try:
        with open(PICKS_PATH, "r", encoding="utf-8") as f:
            picks = json.load(f)

        if not picks.get("is_final"):
            log.warning(f"load_yesterday_picks: 盘中快照(is_final=False), date={picks.get('date','?')}")
            return None

        gainers = picks.get("top100_gainers", [])
        losers = picks.get("top100_losers", [])
        picks_date = picks.get("date", "未知")
        log.info(f"load_yesterday_picks: 加载成功 ({picks_date}, 涨{len(gainers)}/跌{len(losers)})")
        return picks

    except Exception as e:
        log.error(f"load_yesterday_picks: 加载失败: {e}")
        return None



def load_picks_from_history(today_date):
    """从 picks_history.json 读取 today_date 之前最近一天的 picks（方案B）
    
    与 load_yesterday_picks 的区别：
    - 不依赖 yesterday_picks.json（该文件存在覆盖时序问题）
    - 直接从 picks_history.json 按日期索引，找 < today_date 的最近一天
    - 无论何时运行（15:10/16:45/手动），结果一致
    """
    log.info(f"load_picks_from_history: today_date={today_date}")
    hist_path = os.path.join(DATA_DIR, "picks_history.json")
    if not os.path.exists(hist_path):
        log.warning("load_picks_from_history: picks_history.json不存在")
        return None

    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception as e:
        log.error(f"load_picks_from_history: 加载失败: {e}")
        return None

    all_dates = sorted(history.keys())
    log.debug(f"load_picks_from_history: 全部日期={all_dates}")

    # 找 < today_date 的最近一天
    candidates = sorted([d for d in history if d < today_date], reverse=True)
    log.debug(f"load_picks_from_history: candidates(< {today_date})={candidates}")

    if not candidates:
        log.warning(f"load_picks_from_history: 无 {today_date} 之前的数据 (全部日期={all_dates})")
        return None

    pick_date = candidates[0]
    entry = history[pick_date]
    # 统一格式：确保有 date 字段
    entry["date"] = pick_date
    gainers = entry.get("top100_gainers", [])
    losers = entry.get("top100_losers", [])
    log.info(f"load_picks_from_history: 选中 {pick_date} (涨{len(gainers)}/跌{len(losers)}), 跳过日期={[d for d in all_dates if d >= today_date]}")
    return entry


def backfill_picks_history_t1(picks_date, mom_returns, rev_returns):
    """将 T+1 收益回写到 picks_history.json 对应日期条目

    Args:
        picks_date: picks 的日期（str, "2026-04-10"）
        mom_returns: {"avg": float, "median": float, "up_count": int, "matched": int}
        rev_returns: 同上
    """
    log.info(f"backfill_picks_history_t1: picks_date={picks_date}")
    hist_path = os.path.join(DATA_DIR, "picks_history.json")
    if not os.path.exists(hist_path) or picks_date is None:
        log.debug("backfill_picks_history_t1: 文件不存在或picks_date=None, 跳过")
        return

    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception as e:
        log.warning(f"backfill_picks_history_t1: 读取失败: {e}")
        return

    if picks_date not in history:
        log.warning(f"backfill_picks_history_t1: {picks_date} 不在 picks_history 中 (可用={sorted(history.keys())})")
        return

    entry = history[picks_date]
    if entry.get("momentum_avg_return") is not None:
        log.info(f"backfill_picks_history_t1: {picks_date} 已有 T+1 数据 (mom_avg={entry['momentum_avg_return']}), 跳过")
        return

    entry["momentum_avg_return"] = mom_returns.get("avg")
    entry["momentum_median_return"] = mom_returns.get("median")
    entry["momentum_up_count"] = mom_returns.get("up_count", 0)
    entry["momentum_matched"] = mom_returns.get("matched", 0)
    entry["reversion_avg_return"] = rev_returns.get("avg")
    entry["reversion_median_return"] = rev_returns.get("median")
    entry["reversion_up_count"] = rev_returns.get("up_count", 0)
    entry["reversion_matched"] = rev_returns.get("matched", 0)

    try:
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        log.info(f"backfill_picks_history_t1: {picks_date} 回填成功 "
                 f"追涨avg={mom_returns.get('avg')}% 抄底avg={rev_returns.get('avg')}%")
    except Exception as e:
        log.error(f"backfill_picks_history_t1: 写入失败: {e}")


def find_yesterday_limit_up_codes():
    """从昨日归档 pkl 中找真涨停股代码，返回 set"""
    log.debug("find_yesterday_limit_up_codes: 开始")
    if not os.path.isdir(ARCHIVE_SPOT_DIR):
        log.debug("find_yesterday_limit_up_codes: archive目录不存在")
        return set()

    today_str = datetime.now().strftime("%Y%m%d")
    pkls = sorted(os.listdir(ARCHIVE_SPOT_DIR), reverse=True)
    yd_path = None
    for f in pkls:
        if f.startswith("spot_") and f.endswith(".pkl"):
            pkl_date = f[5:13]
            if pkl_date < today_str:
                yd_path = os.path.join(ARCHIVE_SPOT_DIR, f)
                log.debug(f"find_yesterday_limit_up_codes: 昨日归档 → {f}")
                break

    if yd_path is None:
        log.warning("find_yesterday_limit_up_codes: 无昨日归档pkl")
        return set()

    try:
        with open(yd_path, "rb") as f:
            cache = pickle.load(f)
        yd_df = cache.get("df")
        if yd_df is None or len(yd_df) == 0:
            log.warning("find_yesterday_limit_up_codes: 昨日pkl数据为空")
            return set()

        codes = find_limit_up_codes(yd_df, exclude_bj=True)
        log.info(f"find_yesterday_limit_up_codes: {len(codes)}只 (from {os.path.basename(yd_path)})")
        return codes

    except Exception as e:
        log.warning(f"find_yesterday_limit_up_codes: 读取失败: {e}")
        return set()


def load_supplementary_data(df=None):
    """
    从已有 JSON 补充热度和趋势字段：
    - ashare_movers.json → 指数涨跌幅
    - ashare_overview.json → volume_total, csi1000
    - derived_intraday.json → 指数 fallback
    - kpi_history.json → 30日排名
    """
    log.info("load_supplementary_data: 开始")
    sup = {}

    # 指数（ashare_movers.json）
    movers_path = os.path.join(DATA_DIR, "ashare_movers.json")
    if os.path.exists(movers_path):
        try:
            with open(movers_path, "r", encoding="utf-8") as f:
                movers = json.load(f)
            indices = movers.get("indices", {})
            sup["sh_change_pct"]  = indices.get("sh", {}).get("change_pct")
            sup["sz_change_pct"]  = indices.get("sz", {}).get("change_pct")
            sup["cyb_change_pct"] = indices.get("cyb", {}).get("change_pct")
            log.debug(f"load_supplementary_data: movers指数 sh={sup.get('sh_change_pct')} sz={sup.get('sz_change_pct')} cyb={sup.get('cyb_change_pct')}")
        except Exception as e:
            log.warning(f"load_supplementary_data: movers.json读取失败: {e}")
    else:
        log.debug("load_supplementary_data: ashare_movers.json 不存在")

    # 中证1000 + volume_total（ashare_overview.json）
    overview_path = os.path.join(DATA_DIR, "ashare_overview.json")
    if os.path.exists(overview_path):
        try:
            with open(overview_path, "r", encoding="utf-8") as f:
                overview = json.load(f)
            cap_idx = overview.get("cap_indices", {})
            sup["csi1000_change_pct"] = cap_idx.get("small", {}).get("change_pct")

            # v4.9.1: 只取 volume_total，up/down/limit 由 stock_filter 覆盖
            kpi = overview.get("kpi", {})
            sup["volume_total"] = kpi.get("volume_total")
            sup["limit_up"] = kpi.get("limit_up")
            sup["limit_down"] = kpi.get("limit_down")

            # volume_total fallback: 从 spot df 现场计算
            if sup["volume_total"] is None and df is not None:
                col_amount = next((c for c in df.columns if "成交额" in c), None)
                col_price  = next((c for c in df.columns if "最新价" in c), None)
                if col_amount:
                    valid = df[df[col_price] > 0] if col_price else df
                    sup["volume_total"] = round(valid[col_amount].sum() / 1e8, 1)
                    log.info(f"load_supplementary_data: volume_total fallback从spot计算 = {sup['volume_total']}亿")
            log.info(f"load_supplementary_data: KPI volume_total={sup['volume_total']}, csi1000={sup.get('csi1000_change_pct')}")
        except Exception as e:
            log.warning(f"load_supplementary_data: overview.json读取失败: {e}")
    else:
        log.debug("load_supplementary_data: ashare_overview.json 不存在")

    # v4.3: 指数 null 时从 derived_intraday.json 补（腾讯 fallback 源）
    idx_keys = ["sh_change_pct", "sz_change_pct", "cyb_change_pct", "csi1000_change_pct"]
    null_idx = [k for k in idx_keys if sup.get(k) is None]
    if null_idx:
        log.debug(f"load_supplementary_data: 指数缺失 {null_idx}, 尝试derived_intraday补充")
        derived_path = os.path.join(DATA_DIR, "derived_intraday.json")
        if os.path.exists(derived_path):
            try:
                with open(derived_path, "r", encoding="utf-8") as f:
                    derived = json.load(f)
                snapshots = derived.get("snapshots", [])
                if snapshots:
                    latest = snapshots[-1]
                    filled_keys = []
                    for k in idx_keys:
                        if sup.get(k) is None and latest.get(k) is not None:
                            sup[k] = latest[k]
                            filled_keys.append(k)
                    filled = sum(1 for k in idx_keys if sup.get(k) is not None)
                    log.info(f"load_supplementary_data: derived_intraday补充 {filled_keys} ({filled}/4)")
                else:
                    log.debug("load_supplementary_data: derived_intraday snapshots为空")
            except Exception as e:
                log.warning(f"load_supplementary_data: derived_intraday.json读取失败: {e}")

    # 30日排名（kpi_history.json）
    hist_path = os.path.join(DATA_DIR, "kpi_history.json")
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                hist = json.load(f)
            if hist and sup.get("volume_total") is not None:
                vols = [h.get("volume_total", 0) for h in hist if h.get("volume_total")]
                rank = sum(1 for v in vols if v < sup["volume_total"])
                sup["volume_rank_30d"] = rank
                log.debug(f"load_supplementary_data: volume_rank_30d={rank} (对比{len(vols)}天)")
        except Exception as e:
            log.warning(f"load_supplementary_data: kpi_history.json读取失败: {e}")

    # 最终汇总
    final_idx = {k: sup.get(k) for k in idx_keys}
    still_null = [k for k, v in final_idx.items() if v is None]
    if still_null:
        log.warning(f"load_supplementary_data: 指数仍缺失 {still_null}")
    log.info(f"load_supplementary_data: 完成, keys={list(sup.keys())}")

    return sup


def read_new_high_low_diff(trade_date):
    """
    从 ashare_overview.json 读新高-新低差。
    需要 overview.date == trade_date 且 delayed_at 存在。
    返回 int 或 None。
    """
    log.debug(f"read_new_high_low_diff: trade_date={trade_date}")
    overview_path = os.path.join(DATA_DIR, "ashare_overview.json")
    if not os.path.exists(overview_path):
        log.debug("read_new_high_low_diff: overview不存在")
        return None

    try:
        with open(overview_path, "r", encoding="utf-8") as f:
            overview = json.load(f)
        overview_date = overview.get("date", "")
        has_delayed = overview.get("delayed_at") is not None
        kpi = overview.get("kpi", {})

        if trade_date and overview_date == trade_date and has_delayed:
            hi = kpi.get("high_year", 0) or 0
            lo = kpi.get("low_year", 0) or 0
            diff = hi - lo
            log.info(f"read_new_high_low_diff: {diff} (年高{hi}/年低{lo}, date={overview_date})")
            return diff
        elif trade_date and overview_date == trade_date:
            log.warning(f"read_new_high_low_diff: date匹配但delayed_at=None, THS未就绪")
        else:
            log.debug(f"read_new_high_low_diff: date不匹配 overview={overview_date} vs trade={trade_date}")
    except Exception as e:
        log.warning(f"read_new_high_low_diff: 读取失败: {e}")

    return None


def query_history_for_health(trade_date):
    """
    从 DuckDB 查询最近 5 天的 up_ratio 和 sh_change_pct。
    返回 {"up_ratios": [float], "sh_changes": [float]} 或 None。
    """
    log.debug(f"query_history_for_health: trade_date={trade_date}")
    try:
        import duckdb
    except ImportError:
        log.debug("query_history_for_health: duckdb未安装")
        return None

    if not os.path.exists(DB_PATH):
        log.debug("query_history_for_health: DuckDB不存在")
        return None

    try:
        con = duckdb.connect(DB_PATH, read_only=True)
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        if "regime_daily" not in tables:
            log.debug("query_history_for_health: regime_daily表不存在")
            con.close()
            return None

        rows = con.execute("""
            SELECT up_ratio, sh_change_pct
            FROM regime_daily
            WHERE up_ratio IS NOT NULL
              AND date < ?
            ORDER BY date DESC
            LIMIT 5
        """, [trade_date]).fetchall()
        con.close()

        if len(rows) < 2:
            log.debug(f"query_history_for_health: 数据不足 ({len(rows)}行)")
            return None

        result = {
            "up_ratios":  [r[0] for r in rows if r[0] is not None],
            "sh_changes": [r[1] for r in rows if r[1] is not None],
        }
        log.debug(f"query_history_for_health: {len(rows)}行, up_ratios={result['up_ratios'][:3]}")
        return result
    except Exception as e:
        log.warning(f"query_history_for_health: DuckDB查询失败: {e}")
        return None


# ══════════════════════════════════════════════
# 2. 归档 pkl IO（前置分析用）
# ══════════════════════════════════════════════

def load_archive_spot(date_str):
    """加载指定日期的归档 pkl，返回 DataFrame 或 None"""
    fname = f"spot_{date_str.replace('-', '')}.pkl"
    path = os.path.join(ARCHIVE_SPOT_DIR, fname)
    if not os.path.exists(path):
        log.debug(f"load_archive_spot: {fname} 不存在")
        return None
    try:
        with open(path, "rb") as f:
            cache = pickle.load(f)
        df = cache.get("df")
        if df is not None and len(df) > 0:
            log.debug(f"load_archive_spot: {fname} → {len(df)}行")
            return df
        log.debug(f"load_archive_spot: {fname} 数据为空")
        return None
    except Exception as e:
        log.warning(f"load_archive_spot: {fname} 加载失败: {e}")
        return None


def get_prior_archive_dates(today_str, n=6):
    """获取今日之前最近 n 个归档日期（降序）"""
    if not os.path.isdir(ARCHIVE_SPOT_DIR):
        log.debug("get_prior_archive_dates: archive目录不存在")
        return []
    dates = []
    for f in os.listdir(ARCHIVE_SPOT_DIR):
        if f.startswith("spot_") and f.endswith(".pkl"):
            d = f[5:13]
            try:
                ds = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                if ds < today_str:
                    dates.append(ds)
            except Exception:
                continue
    dates.sort(reverse=True)
    log.debug(f"get_prior_archive_dates: {len(dates)} 个可用 (取前{n}), 最近={dates[:3] if dates else '无'}")
    return dates[:n]


def load_prior_close_maps(today_date):
    """
    加载前置分析需要的归档数据，返回 4 个映射。

    返回 (chg_map_t1, close_map_t1, close_map_t4, close_map_t6)
    任何一个加载失败的返回 {}。
    """
    log.info(f"load_prior_close_maps: today_date={today_date}")
    prior_dates = get_prior_archive_dates(today_date, n=6)
    if not prior_dates:
        log.warning("load_prior_close_maps: 无归档pkl, 前置分析全null")
        return {}, {}, {}, {}

    log.info(f"load_prior_close_maps: 可用归档 {len(prior_dates)}天 [{prior_dates[0]}..{prior_dates[-1]}]")

    t1_df = load_archive_spot(prior_dates[0]) if len(prior_dates) >= 1 else None
    t4_df = load_archive_spot(prior_dates[3]) if len(prior_dates) >= 4 else None
    t6_df = load_archive_spot(prior_dates[5]) if len(prior_dates) >= 6 else None

    loaded = []
    if t1_df is not None: loaded.append(f"T-1({prior_dates[0]})")
    if t4_df is not None: loaded.append(f"T-4({prior_dates[3]})")
    if t6_df is not None: loaded.append(f"T-6({prior_dates[5]})")
    log.debug(f"load_prior_close_maps: 已加载 {loaded}")

    chg_map_t1  = build_today_chg_map(t1_df) if t1_df is not None else {}
    close_map_t1 = build_close_map(t1_df) if t1_df is not None else {}
    close_map_t4 = build_close_map(t4_df) if t4_df is not None else {}
    close_map_t6 = build_close_map(t6_df) if t6_df is not None else {}

    return chg_map_t1, close_map_t1, close_map_t4, close_map_t6


# ══════════════════════════════════════════════
# 3. DuckDB 写入 + JSON 导出
# ══════════════════════════════════════════════

# ── 列定义常量 ──

TIER_COLUMNS = []
for _pf in ["mom", "rev"]:
    for _t in TIERS:
        for _m in ["avg", "median", "up", "n"]:
            _typ = "DOUBLE" if _m in ("avg", "median") else "INTEGER"
            TIER_COLUMNS.append((f"{_pf}_{_t}_{_m}", _typ))

INDICATOR_COLUMNS = [
    ("zt_premium_avg",       "DOUBLE"),
    ("cap_scissors",         "DOUBLE"),
    ("median_change_pct",    "DOUBLE"),
    ("volume_price_ratio",   "DOUBLE"),
    ("change_pct_stdev",     "DOUBLE"),
    ("volume_concentration", "DOUBLE"),
    ("extreme_ratio",        "DOUBLE"),
    ("high_price_count",     "INTEGER"),
    ("high_price_avg_chg",   "DOUBLE"),
    ("high_price_up_count",  "INTEGER"),
    ("breadth_5d_avg",       "DOUBLE"),
    ("volatility_5d",        "DOUBLE"),
    # v5.3: 涨幅质量指标（替换市场健康区涨停/跌停比 + 新高-新低差的前端展示）
    ("vwap_bias_median",           "DOUBLE"),
    ("intraday_strength_median",   "DOUBLE"),
]

SECTOR_DIST_COLUMNS = [
    ("sector_dist_gainers", "JSON"),
    ("sector_dist_losers",  "JSON"),
    ("concentration_hhi",   "DOUBLE"),
]

PRIOR_COLUMNS = []
for _pf in ["gn", "ls"]:
    for _win in [1, 3, 5]:
        for _met, _typ in [("same", "DOUBLE"), ("avg", "DOUBLE"), ("med", "DOUBLE"), ("strong", "DOUBLE")]:
            PRIOR_COLUMNS.append((f"{_pf}_prev{_win}_{_met}", _typ))

# v5.1: prior 直方图字段 (9 桶分布), 存为 JSON 字符串 → 前端反序列化为 array
PRIOR_HIST_COLUMNS = []
for _pf in ["gn", "ls"]:
    for _win in [1, 3, 5]:
        PRIOR_HIST_COLUMNS.append((f"{_pf}_prev{_win}_hist", "VARCHAR"))

ALL_NEW_COLUMNS = TIER_COLUMNS + INDICATOR_COLUMNS + SECTOR_DIST_COLUMNS + PRIOR_COLUMNS + PRIOR_HIST_COLUMNS + [("return_date", "DATE")]

DDL = """
CREATE TABLE IF NOT EXISTS regime_daily (
    date DATE PRIMARY KEY,

    -- 热度
    volume_total DOUBLE,
    volume_rank_30d INTEGER,
    limit_up INTEGER,
    limit_down INTEGER,
    up_count INTEGER,
    down_count INTEGER,
    up_ratio DOUBLE,

    -- 趋势：指数
    sh_change_pct DOUBLE,
    sz_change_pct DOUBLE,
    cyb_change_pct DOUBLE,
    csi1000_change_pct DOUBLE,

    -- T+1次日收益（整体）
    momentum_avg_return DOUBLE,
    momentum_median_return DOUBLE,
    momentum_up_count INTEGER,
    momentum_matched INTEGER,
    reversion_avg_return DOUBLE,
    reversion_median_return DOUBLE,
    reversion_up_count INTEGER,
    reversion_matched INTEGER,

    -- 分档T+1
    mom_micro_avg DOUBLE, mom_micro_median DOUBLE, mom_micro_up INTEGER, mom_micro_n INTEGER,
    mom_small_avg DOUBLE, mom_small_median DOUBLE, mom_small_up INTEGER, mom_small_n INTEGER,
    mom_mid_avg DOUBLE,   mom_mid_median DOUBLE,   mom_mid_up INTEGER,   mom_mid_n INTEGER,
    mom_large_avg DOUBLE, mom_large_median DOUBLE, mom_large_up INTEGER, mom_large_n INTEGER,
    rev_micro_avg DOUBLE, rev_micro_median DOUBLE, rev_micro_up INTEGER, rev_micro_n INTEGER,
    rev_small_avg DOUBLE, rev_small_median DOUBLE, rev_small_up INTEGER, rev_small_n INTEGER,
    rev_mid_avg DOUBLE,   rev_mid_median DOUBLE,   rev_mid_up INTEGER,   rev_mid_n INTEGER,
    rev_large_avg DOUBLE, rev_large_median DOUBLE, rev_large_up INTEGER, rev_large_n INTEGER,

    -- 市值分布
    gainer_micro INTEGER, gainer_small INTEGER,
    gainer_mid INTEGER,   gainer_large INTEGER,
    loser_micro INTEGER,  loser_small INTEGER,
    loser_mid INTEGER,    loser_large INTEGER,

    -- 股价分布
    gainer_p0_10 INTEGER,  gainer_p10_30 INTEGER,
    gainer_p30_50 INTEGER, gainer_p50_100 INTEGER, gainer_p100p INTEGER,
    loser_p0_10 INTEGER,   loser_p10_30 INTEGER,
    loser_p30_50 INTEGER,  loser_p50_100 INTEGER,  loser_p100p INTEGER,

    -- 板块
    sector_count_gainers INTEGER,
    sector_count_losers INTEGER,
    sector_overlap INTEGER,
    top_gainer_sectors JSON,
    top_loser_sectors JSON,
    sector_dist_gainers JSON,
    sector_dist_losers JSON,
    micro_cap_ratio_gainer DOUBLE,

    -- 衍生指标
    zt_premium_avg DOUBLE,
    cap_scissors DOUBLE,
    median_change_pct DOUBLE,
    volume_price_ratio DOUBLE,
    change_pct_stdev DOUBLE,
    volume_concentration DOUBLE,
    extreme_ratio DOUBLE,
    high_price_count INTEGER,
    high_price_avg_chg DOUBLE,
    high_price_up_count INTEGER,

    -- 市场健康
    breadth_5d_avg DOUBLE,
    zt_dt_ratio DOUBLE,
    new_high_low_diff INTEGER,
    volatility_5d DOUBLE,
    -- v5.3: 涨幅质量指标
    vwap_bias_median DOUBLE,
    intraday_strength_median DOUBLE,

    -- 前置分析
    gn_prev1_same DOUBLE, gn_prev1_avg DOUBLE, gn_prev1_med DOUBLE, gn_prev1_strong DOUBLE,
    gn_prev3_same DOUBLE, gn_prev3_avg DOUBLE, gn_prev3_med DOUBLE, gn_prev3_strong DOUBLE,
    gn_prev5_same DOUBLE, gn_prev5_avg DOUBLE, gn_prev5_med DOUBLE, gn_prev5_strong DOUBLE,
    ls_prev1_same DOUBLE, ls_prev1_avg DOUBLE, ls_prev1_med DOUBLE, ls_prev1_strong DOUBLE,
    ls_prev3_same DOUBLE, ls_prev3_avg DOUBLE, ls_prev3_med DOUBLE, ls_prev3_strong DOUBLE,
    ls_prev5_same DOUBLE, ls_prev5_avg DOUBLE, ls_prev5_med DOUBLE, ls_prev5_strong DOUBLE,

    -- 标签 + 元数据
    regime_label VARCHAR,
    picks_date DATE,
    return_date DATE,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _migrate_regime_table(con):
    """迁移：已有 regime_daily 表若缺少新列，自动 ALTER TABLE 添加"""
    try:
        existing = set()
        for row in con.execute("PRAGMA table_info('regime_daily')").fetchall():
            existing.add(row[1])

        added = 0
        added_names = []
        for col_name, col_type in ALL_NEW_COLUMNS:
            if col_name not in existing:
                con.execute(f"ALTER TABLE regime_daily ADD COLUMN {col_name} {col_type}")
                added += 1
                added_names.append(col_name)

        if added:
            log.info(f"_migrate_regime_table: 新增 {added} 列: {added_names}")
        else:
            log.debug("_migrate_regime_table: 无需迁移")
    except Exception as e:
        log.warning(f"_migrate_regime_table: 迁移失败: {e}")


def save_to_duckdb(record):
    """写入 regime_daily 表 + 导出最近 30 条到 regime_history.json"""
    log.info(f"save_to_duckdb: date={record.get('date')}, picks_date={record.get('picks_date')}, return_date={record.get('return_date')}")
    log.debug(f"save_to_duckdb: regime_label={record.get('regime_label')}, mom_avg={record.get('momentum_avg_return')}, rev_avg={record.get('reversion_avg_return')}")

    try:
        import duckdb
    except ImportError:
        log.warning("save_to_duckdb: duckdb未安装, 跳过")
        return False

    if not os.path.exists(DB_PATH):
        log.warning(f"save_to_duckdb: DuckDB不存在 ({DB_PATH})")
        return False

    try:
        t_start = time.time()
        con = duckdb.connect(DB_PATH)
        con.execute(DDL)
        _migrate_regime_table(con)

        # ── 构建 fields / values / placeholders ──
        fields = []
        values = []
        placeholders = []

        def add(name, val, is_json=False):
            fields.append(name)
            values.append(val)
            placeholders.append("json(?)" if is_json else "?")

        add("date", record["date"])
        for f in ["volume_total", "volume_rank_30d", "limit_up", "limit_down",
                   "up_count", "down_count", "up_ratio",
                   "sh_change_pct", "sz_change_pct", "cyb_change_pct", "csi1000_change_pct",
                   "momentum_avg_return", "momentum_median_return", "momentum_up_count", "momentum_matched",
                   "reversion_avg_return", "reversion_median_return", "reversion_up_count", "reversion_matched"]:
            add(f, record.get(f))

        for col_name, _ in TIER_COLUMNS:
            add(col_name, record.get(col_name))

        for f in ["gainer_micro", "gainer_small", "gainer_mid", "gainer_large",
                   "loser_micro", "loser_small", "loser_mid", "loser_large"]:
            add(f, record.get(f))

        for f in ["gainer_p0_10", "gainer_p10_30", "gainer_p30_50", "gainer_p50_100", "gainer_p100p",
                   "loser_p0_10", "loser_p10_30", "loser_p30_50", "loser_p50_100", "loser_p100p"]:
            add(f, record.get(f))

        for f in ["sector_count_gainers", "sector_count_losers", "sector_overlap"]:
            add(f, record.get(f))
        add("top_gainer_sectors", json.dumps(record.get("top_gainer_sectors", []), ensure_ascii=False), is_json=True)
        add("top_loser_sectors",  json.dumps(record.get("top_loser_sectors", []), ensure_ascii=False), is_json=True)
        add("sector_dist_gainers", json.dumps(record.get("sector_dist_gainers", {}), ensure_ascii=False), is_json=True)
        add("sector_dist_losers",  json.dumps(record.get("sector_dist_losers", {}), ensure_ascii=False), is_json=True)
        add("concentration_hhi", record.get("concentration_hhi"))
        add("micro_cap_ratio_gainer", record.get("micro_cap_ratio_gainer"))

        for col_name, _ in INDICATOR_COLUMNS:
            add(col_name, record.get(col_name))

        for col_name, _ in PRIOR_COLUMNS:
            add(col_name, record.get(col_name))

        # v5.1: prior 直方图 (list → JSON string)
        for col_name, _ in PRIOR_HIST_COLUMNS:
            v = record.get(col_name)
            add(col_name, json.dumps(v) if v is not None else None, is_json=True)

        add("regime_label", record.get("regime_label"))
        add("picks_date",   record.get("picks_date"))
        add("return_date",  record.get("return_date"))

        fields.append("fetched_at")
        placeholders.append("CURRENT_TIMESTAMP")

        log.debug(f"save_to_duckdb: INSERT OR REPLACE {len(fields)} 列")

        sql = f"""
            INSERT OR REPLACE INTO regime_daily ({', '.join(fields)})
            VALUES ({', '.join(placeholders)})
        """
        con.execute(sql, values)

        # 导出最近 30 条
        rows = con.execute("""
            SELECT * FROM regime_daily
            ORDER BY date DESC LIMIT 30
        """).fetchdf()
        con.close()

        history = json.loads(rows.to_json(orient="records", date_format="iso", force_ascii=False))

        # 日期截断
        truncated_dates = 0
        for rec in history:
            for dk in ("date", "picks_date", "return_date"):
                v = str(rec.get(dk, "") or "")
                if "T" in v:
                    rec[dk] = v[:10]
                    truncated_dates += 1
        if truncated_dates > 0:
            log.debug(f"save_to_duckdb: 日期T截断 {truncated_dates} 处")

        _json_fields = ("sector_dist_gainers", "sector_dist_losers",
                        "top_gainer_sectors", "top_loser_sectors",
                        "gn_prev1_hist", "gn_prev3_hist", "gn_prev5_hist",
                        "ls_prev1_hist", "ls_prev3_hist", "ls_prev5_hist")
        for rec in history:
            for k in _json_fields:
                v = rec.get(k)
                if isinstance(v, str):
                    try:
                        rec[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        pass

        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - t_start
        log.info(f"save_to_duckdb: 完成 ({elapsed:.1f}s) → DuckDB + regime_history.json ({len(history)}天)")

        # 验证关键字段
        latest = history[0] if history else {}
        log.debug(f"save_to_duckdb: 导出验证 latest → date={latest.get('date')}, picks_date={latest.get('picks_date')}, "
                  f"return_date={latest.get('return_date')}, regime_label={latest.get('regime_label')}")

        return True

    except Exception as e:
        log.error(f"save_to_duckdb: DuckDB写入失败: {e}")
        import traceback
        traceback.print_exc()
        return False
