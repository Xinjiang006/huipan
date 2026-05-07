"""
慧盘 · 百元股趋势追踪器 v1.0 (p100_signals.py)
=============================================
独立第三候选池，与 reversal_tracker_v57 + breakout_tracker 完全并行。

功能定位:
- 池: ≥100¥ 高价趋势股
- 风格: 趋势启动 + 延续 (T+1 ~ T+10)
- 调度: 15:11 generate 单阶段 (错开 reversal 15:10 / breakout 15:10:30)

三层输出:
- Layer 1 粗筛: A 单一 [6,20] - 给 LLM 看动向
- Layer 2 主追踪: A ∩ D' ∩ 距20日低<25% - 实战决策主池
- Layer 3 alert: 金子A / B∩D' - 高亮 tag

26 天回测核心结论:
- L2_v2: 日均 10 只, 强机会率 36.5%, 真死率 9.1%, μ +9.14%
- 去重 αP100 +1.62% (vs D 基线 +1.90%, 但召回 71% > 56%)
- OOS T+3 +3.95% (反过拟合特征)
- 详见 docs/v100-p100-signals.md

依赖:
- utils/pkl_helper.py (统一 pkl 加载)
- utils/paths.py (路径常量)
- 历史 pkl: archive/spot/spot_YYYYMMDD.pkl (≥20 天)
- sector_map.json
"""
from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# 复用 utils
try:
    from utils import pkl_helper
    from utils.pkl_helper import (
        FIELD_CODE, FIELD_NAME, FIELD_LAST, FIELD_CHG_PCT,
        FIELD_HIGH, FIELD_LOW, FIELD_VOL,
    )
    from utils.paths import (
        DATA_DIR, ARCHIVE_DIR, CONFIG_DIR, SPOT_CACHE,
    )
    HAS_UTILS = True
except ImportError:
    HAS_UTILS = False
    # 字段常量降级 - 与生产 pkl 对齐
    FIELD_CODE = '代码'
    FIELD_NAME = '名称'
    FIELD_LAST = '最新价'
    FIELD_CHG_PCT = '涨跌幅'
    FIELD_HIGH = '最高'
    FIELD_LOW = '最低'
    FIELD_VOL = '成交量'

logger = logging.getLogger(__name__)


# ==========================================================================
# 配置
# ==========================================================================

DEFAULT_CONFIG = {
    "version": "1.0",

    # 全局
    "min_price": 100.0,                  # ≥100¥ 池
    "archive_lookback_days": 25,         # 加载 25 天 pkl 保 20 日窗口

    # Layer 1 (A 单一)
    "layer1": {
        "enabled": True,
        "spike_lookback": 10,            # 10 日窗口
        "spike_low": 6.0,                # 单日涨幅 6-20%
        "spike_high": 20.0,
        "max_output": 100,               # 输出上限
    },

    # Layer 2 (A ∩ D' ∩ 距低<25%)
    "layer2": {
        "enabled": True,
        # A 同 layer1
        # D'
        "d_prime_lookback": 3,           # 3 日均值
        "d_prime_z_low": 0.5,
        "d_prime_z_high": 2.0,
        "d_prime_min_pool": 20,          # 池规模 < 20 不算 z
        # 距 20 日低
        "dist_low_lookback": 20,
        "dist_low_max": 25.0,            # 距低 < 25%
        "max_output": 30,
    },

    # Layer 3 alerts
    "layer3": {
        "enabled": True,
        # 金子A: 距低<15 + spike5d≥8 + today≤5
        "gold_a_dist_low_max": 15.0,
        "gold_a_spike_min": 8.0,
        "gold_a_today_max": 5.0,
        # B∩D': 距高 ∈ [-8,-5] + today ∈ [0, 2]
        "bd_dist_high_low": -8.0,
        "bd_dist_high_high": -5.0,
        "bd_today_low": 0.0,
        "bd_today_high": 2.0,
        "bd_lookback": 20,
    },

    # 持有追踪 (配套机制)
    "holding": {
        "enabled": True,
        "max_holding_days": 10,          # 持有 10 天后退出
        "stop_loss_dd": -8.0,            # 持有期回撤 < -8% 警告
    },

    # D 基线 (对照, 不入主池)
    "d_baseline": {
        "enabled": True,
        "lookback": 5,
        "z_low": 1.0,
        "z_high": 1.5,
    },
}


# ==========================================================================
# 路径与常量
# ==========================================================================

if HAS_UTILS:
    SCRIPT_NAME = "p100_signals"
    OUTPUT_FILE = DATA_DIR / "p100_watchlist.json"
    HOLDING_FILE = DATA_DIR / "p100_holding.json"
    CONFIG_FILE = CONFIG_DIR / "p100_config.json"
    SECTOR_MAP_FILE = DATA_DIR / "sector_map.json"
else:
    DATA_DIR = Path("static/data")
    ARCHIVE_DIR = DATA_DIR / "archive" / "spot"
    CONFIG_DIR = Path("config")
    SPOT_CACHE = DATA_DIR / ".spot_cache.pkl"
    OUTPUT_FILE = DATA_DIR / "p100_watchlist.json"
    HOLDING_FILE = DATA_DIR / "p100_holding.json"
    CONFIG_FILE = CONFIG_DIR / "p100_config.json"
    SECTOR_MAP_FILE = DATA_DIR / "sector_map.json"


# ==========================================================================
# 工具函数
# ==========================================================================

def _setup_logger():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(h)


def _norm_code(code: str) -> str:
    """sh600519 → 600519"""
    if isinstance(code, str) and code[:2] in ("sh", "sz", "bj"):
        return code[2:]
    return code


def _load_config() -> dict:
    """加载配置文件 (mtime 热加载, 不存在用默认)"""
    if not CONFIG_FILE.exists():
        logger.info(f"使用 DEFAULT_CONFIG (无 {CONFIG_FILE.name})")
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user_cfg = json.load(f)
        # 浅合并 (顶层) + 子节点深合并
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        logger.info(f"已加载配置 {CONFIG_FILE.name}")
        return merged
    except Exception as e:
        logger.warning(f"配置文件读取失败 {e}, 使用 DEFAULT_CONFIG")
        return DEFAULT_CONFIG


def _load_sector_map() -> dict[str, str]:
    """加载 sector_map.json -> {code(无前缀): sector}"""
    if not SECTOR_MAP_FILE.exists():
        logger.warning(f"未找到 {SECTOR_MAP_FILE}, 板块字段将为'未知'")
        return {}
    try:
        with open(SECTOR_MAP_FILE, encoding="utf-8") as f:
            sm = json.load(f)
        return sm.get("map", {})
    except Exception as e:
        logger.warning(f"sector_map 加载失败 {e}")
        return {}


def _load_today_spot() -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """加载今日 spot. 返回 (df, date_str)
    优先 SPOT_CACHE.pkl, fallback 最新归档 pkl (mtime 校验防"幽灵今日")"""
    today = datetime.now().strftime("%Y%m%d")

    # 1. 尝试 SPOT_CACHE
    if SPOT_CACHE.exists():
        try:
            mtime = datetime.fromtimestamp(SPOT_CACHE.stat().st_mtime)
            cache_date = mtime.strftime("%Y%m%d")
            # 周末判断: 若 mtime 是周末但今天是周一+, 用 cache 但日期标记为 mtime 那天
            df = pd.read_pickle(SPOT_CACHE)
            if isinstance(df, dict) and "df" in df:
                df = df["df"]
            df = df.copy()
            df["code_n"] = df[FIELD_CODE].apply(_norm_code)
            return df, cache_date
        except Exception as e:
            logger.warning(f"SPOT_CACHE 读取失败 {e}, 尝试归档")

    # 2. fallback: 最新归档 pkl
    pkls = sorted(ARCHIVE_DIR.glob("spot_*.pkl"))
    if not pkls:
        logger.error("未找到任何 spot_*.pkl")
        return None, None
    latest = pkls[-1]
    date_str = latest.stem.replace("spot_", "")
    try:
        d = pd.read_pickle(latest)
        df = d["df"] if isinstance(d, dict) else d
        df = df.copy()
        df["code_n"] = df[FIELD_CODE].apply(_norm_code)
        logger.info(f"使用归档 {latest.name}")
        return df, date_str
    except Exception as e:
        logger.error(f"归档读取失败 {e}")
        return None, None


def _load_archive_panels(today_date: str, lookback: int) -> dict[str, pd.DataFrame]:
    """加载 lookback 天归档 pkl, 不含 today_date 当天.
    返回 {date_str: df}"""
    pkls = sorted(ARCHIVE_DIR.glob("spot_*.pkl"))
    arch = {}
    for p in pkls:
        d = p.stem.replace("spot_", "")
        if d >= today_date:
            continue
        try:
            obj = pd.read_pickle(p)
            df = obj["df"] if isinstance(obj, dict) else obj
            df = df.copy()
            df["code_n"] = df[FIELD_CODE].apply(_norm_code)
            arch[d] = df
        except Exception:
            continue
    # 取最新的 lookback 天
    sorted_dates = sorted(arch.keys())[-lookback:]
    return {d: arch[d] for d in sorted_dates}


def _build_panels(today_df: pd.DataFrame, today_date: str,
                  archive: dict[str, pd.DataFrame]) -> dict:
    """构建 close/pct/high/low/vol 的宽表 (index=code, cols=date)"""
    all_dates = sorted(archive.keys()) + [today_date]
    all_codes: set[str] = set()
    for df in [today_df] + list(archive.values()):
        all_codes.update(df["code_n"].tolist())
    all_codes = sorted(all_codes)

    def _build(field: str) -> pd.DataFrame:
        cols = {}
        for d, df in archive.items():
            cols[d] = df.set_index("code_n")[field]
        cols[today_date] = today_df.set_index("code_n")[field]
        return pd.DataFrame(cols).reindex(all_codes)

    return {
        "close": _build(FIELD_LAST),
        "pct": _build(FIELD_CHG_PCT),
        "high": _build(FIELD_HIGH),
        "low": _build(FIELD_LOW),
        "vol": _build(FIELD_VOL),
        "dates": all_dates,
        "today_date": today_date,
        "today_df": today_df,
    }


# ==========================================================================
# 因子计算
# ==========================================================================

def factor_A(panels: dict, today: str, cfg: dict) -> pd.Series:
    """A 因子: lookback 日内任一日 spike ∈ [low, high]"""
    close = panels["close"]; pct = panels["pct"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(False, index=close.index)
    t_idx = dates.index(today)
    lookback = cfg["spike_lookback"]
    if t_idx < lookback - 1:
        return pd.Series(False, index=close.index)
    win = dates[t_idx - lookback + 1: t_idx + 1]
    sub = pct[win]
    return ((sub >= cfg["spike_low"]) & (sub <= cfg["spike_high"])).any(axis=1).fillna(False)


def factor_D_prime(panels: dict, today: str, cfg: dict, min_price: float) -> pd.Series:
    """D' 因子: 3 日均值 (不截尾) 在 ≥min_price 池内的 z-score ∈ [z_low, z_high]"""
    close = panels["close"]; pct = panels["pct"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(False, index=close.index)
    t_idx = dates.index(today)
    lookback = cfg["d_prime_lookback"]
    if t_idx < lookback - 1:
        return pd.Series(False, index=close.index)
    win = dates[t_idx - lookback + 1: t_idx + 1]
    sub = pct[win]
    arr = sub.values
    has_nan = np.isnan(arr).any(axis=1)
    mean_arr = np.nanmean(arr, axis=1)
    mean_arr[has_nan] = np.nan
    mean_full = pd.Series(mean_arr, index=sub.index)

    pool_mask = close[today] >= min_price
    pool_mean = mean_full[pool_mask].dropna()
    if len(pool_mean) < cfg["d_prime_min_pool"]:
        return pd.Series(False, index=close.index)
    mu = pool_mean.mean()
    sigma = pool_mean.std()
    if sigma == 0 or np.isnan(sigma):
        return pd.Series(False, index=close.index)
    z_full = (mean_full - mu) / sigma
    return ((z_full >= cfg["d_prime_z_low"]) &
            (z_full <= cfg["d_prime_z_high"])).fillna(False)


def factor_D_baseline(panels: dict, today: str, cfg: dict, min_price: float) -> pd.Series:
    """D 基线: 5 日截尾 z-score ∈ [1.0, 1.5]"""
    close = panels["close"]; pct = panels["pct"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(False, index=close.index)
    t_idx = dates.index(today)
    lookback = cfg["lookback"]
    if t_idx < lookback - 1:
        return pd.Series(False, index=close.index)
    win = dates[t_idx - lookback + 1: t_idx + 1]
    sub = pct[win]
    arr = sub.values
    has_nan = np.isnan(arr).any(axis=1)
    row_sum = np.nansum(arr, axis=1)
    row_max = np.nanmax(arr, axis=1)
    row_min = np.nanmin(arr, axis=1)
    tmean_arr = (row_sum - row_max - row_min) / max(1, lookback - 2)
    tmean_arr[has_nan] = np.nan
    tmean = pd.Series(tmean_arr, index=sub.index)

    pool_mask = close[today] >= min_price
    pool_t = tmean[pool_mask].dropna()
    if len(pool_t) < 20:
        return pd.Series(False, index=close.index)
    mu = pool_t.mean()
    sigma = pool_t.std()
    if sigma == 0 or np.isnan(sigma):
        return pd.Series(False, index=close.index)
    z = (tmean - mu) / sigma
    return ((z >= cfg["z_low"]) & (z <= cfg["z_high"])).fillna(False)


def calc_dist_low(panels: dict, today: str, lookback: int) -> pd.Series:
    """距 N 日低的百分比. 数据不足用现有最长窗口"""
    close = panels["close"]; low = panels["low"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(np.nan, index=close.index)
    t_idx = dates.index(today)
    if t_idx < 4:
        return pd.Series(np.nan, index=close.index)
    win_start = max(0, t_idx - lookback + 1)
    win = dates[win_start: t_idx + 1]
    l_min = low[win].min(axis=1)
    return (close[today] - l_min) / l_min * 100


def calc_dist_high(panels: dict, today: str, lookback: int,
                    min_lookback: int = 15) -> pd.Series:
    """距 N 日高的百分比 (B 因子用), 与生产 reversal_tracker 对齐"""
    close = panels["close"]; high = panels["high"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(np.nan, index=close.index)
    t_idx = dates.index(today)
    if t_idx + 1 < min_lookback:
        return pd.Series(np.nan, index=close.index)
    win_start = max(0, t_idx + 1 - lookback)
    win = dates[win_start: t_idx + 1]
    h_max = high[win].max(axis=1)
    return (close[today] - h_max) / h_max * 100


def calc_max_spike_5d(panels: dict, today: str) -> pd.Series:
    """近 5 日内最大单日涨幅 (含今日)"""
    pct = panels["pct"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(np.nan, index=pct.index)
    t_idx = dates.index(today)
    if t_idx < 4:
        return pd.Series(np.nan, index=pct.index)
    win = dates[t_idx - 4: t_idx + 1]
    return pct[win].max(axis=1)


def calc_vol_ratio(panels: dict, today: str, lookback: int = 10) -> pd.Series:
    """量比: today_vol / 过去 lookback 日均量 (不含今日)"""
    vol = panels["vol"]
    dates = panels["dates"]
    if today not in dates:
        return pd.Series(np.nan, index=vol.index)
    t_idx = dates.index(today)
    if t_idx < lookback:
        return pd.Series(np.nan, index=vol.index)
    win = dates[t_idx - lookback: t_idx]
    avg = vol[win].mean(axis=1)
    return vol[today] / avg.replace(0, np.nan)


# ==========================================================================
# 候选生成
# ==========================================================================

def generate_candidates(config: Optional[dict] = None) -> bool:
    """主入口: 生成 p100_watchlist.json"""
    _setup_logger()
    logger.info("═══ 百元股趋势追踪器 v1.0 · 候选生成 ═══")

    if config is None:
        config = _load_config()
    min_price = config["min_price"]

    # 1. 加载数据
    today_df, today_date = _load_today_spot()
    if today_df is None or today_date is None:
        logger.error("今日 spot 加载失败, 退出")
        return False

    archive = _load_archive_panels(today_date, config["archive_lookback_days"])
    if len(archive) < 11:
        logger.error(f"归档 pkl 不足 11 天 (当前 {len(archive)}), 无法计算 A 因子")
        return False
    logger.info(f"装载: 归档 {len(archive)} 天 ({sorted(archive.keys())[0]} ~ "
                f"{sorted(archive.keys())[-1]}) + 今日 {today_date}")

    sector_map = _load_sector_map()
    panels = _build_panels(today_df, today_date, archive)
    close_t = panels["close"][today_date]

    # 2. 池规模
    pool_mask = close_t >= min_price
    pool_size = int(pool_mask.sum())
    logger.info(f"≥{min_price}¥ 池规模: {pool_size}")
    if pool_size < 20:
        logger.error(f"池规模 < 20, 终止")
        return False

    # 3. 计算各因子
    mask_A = factor_A(panels, today_date, config["layer1"])
    mask_Dp = factor_D_prime(panels, today_date, config["layer2"], min_price)
    dist_low = calc_dist_low(panels, today_date,
                              config["layer2"]["dist_low_lookback"])
    mask_dist_low = (dist_low < config["layer2"]["dist_low_max"]).fillna(False)

    # B 因子 (用于 BD alert)
    bd_cfg = config["layer3"]
    dist_high = calc_dist_high(panels, today_date, bd_cfg["bd_lookback"])
    today_pct_s = panels["pct"][today_date]
    mask_B = ((dist_high >= bd_cfg["bd_dist_high_low"]) &
              (dist_high <= bd_cfg["bd_dist_high_high"]) &
              (today_pct_s >= bd_cfg["bd_today_low"]) &
              (today_pct_s <= bd_cfg["bd_today_high"])).fillna(False)

    # 5 日 max spike (用于金子A 和展示)
    max_spike_5d = calc_max_spike_5d(panels, today_date)

    # D 基线 (对照)
    mask_D_base = (factor_D_baseline(panels, today_date,
                                       config["d_baseline"], min_price)
                   if config["d_baseline"]["enabled"]
                   else pd.Series(False, index=close_t.index))

    # 量比
    vol_ratio = calc_vol_ratio(panels, today_date)

    # 4. 三层组合
    # Layer 1: A only (在池内)
    L1 = mask_A & pool_mask
    # Layer 2: A ∩ D' ∩ 距低<25 (在池内)
    L2 = L1 & mask_Dp & mask_dist_low
    # Layer 3 alerts
    # 金子A: 距低<15 + spike5d≥8 + today≤5
    gold_a_mask = ((dist_low < bd_cfg["gold_a_dist_low_max"]) &
                   (max_spike_5d >= bd_cfg["gold_a_spike_min"]) &
                   (today_pct_s <= bd_cfg["gold_a_today_max"]) &
                   pool_mask).fillna(False)
    # B∩D': B 因子 + D' 命中 (主线龙头二买)
    bd_mask = mask_B & mask_Dp & pool_mask

    logger.info(f"因子命中: L1(A){int(L1.sum())} / L2(A∩D'∩距低<25){int(L2.sum())} / "
                f"金A{int(gold_a_mask.sum())} / B∩D'{int(bd_mask.sum())} / "
                f"D基线{int((mask_D_base & pool_mask).sum())}")

    # 5. 组装 candidates
    candidates: list[dict] = []

    def _make_row(code: str, layer: int) -> dict:
        sec = sector_map.get(code, "未知")
        p = float(close_t[code])
        today_p = float(today_pct_s[code]) if not pd.isna(today_pct_s[code]) else None
        d_low = (float(dist_low[code]) if code in dist_low.index
                 and not pd.isna(dist_low[code]) else None)
        spike5 = (float(max_spike_5d[code]) if code in max_spike_5d.index
                  and not pd.isna(max_spike_5d[code]) else None)
        vr = (float(vol_ratio[code]) if code in vol_ratio.index
              and not pd.isna(vol_ratio[code]) else None)

        signals = []
        priority = 5  # 默认优先级
        notes = []

        if bool(L1.get(code, False)):
            signals.append("A")
        if bool(mask_Dp.get(code, False)):
            signals.append("D'")
        if bool(L2.get(code, False)):
            signals.append("L2_v2")
            priority = min(priority, 4)
        if bool(gold_a_mask.get(code, False)):
            signals.append("金子A")
            priority = min(priority, 2)
            notes.append("距低<15 + spike≥8 + 温和涨")
        if bool(bd_mask.get(code, False)):
            signals.append("B∩D'")
            priority = min(priority, 1)
            notes.append("主线龙头二买位置")
        if bool(mask_D_base.get(code, False)) and pool_mask.get(code, False):
            signals.append("D基线")
            if "L2_v2" in signals:
                priority = min(priority, 3)
                notes.append("L2 + D 基线高精度叠加")

        # 警告: 当日涨停接力陷阱 + 板块陷阱
        warnings = []
        if today_p is not None and today_p >= 10.0 and spike5 is not None and abs(spike5 - today_p) < 0.5:
            warnings.append("⚠涨停接力(spike=今日)")
        if today_p is not None and today_p >= 10.0:
            warnings.append("⚠当日≥10%买顶风险")
        if sec in ("医药生物", "计算机", "汽车"):
            warnings.append(f"⚠{sec}板块陷阱率高")

        return {
            "code": code,
            "name": (today_df.loc[today_df["code_n"] == code, FIELD_NAME].iloc[0]
                     if (today_df["code_n"] == code).any() else ""),
            "sector": sec,
            "price": round(p, 2),
            "today_pct": round(today_p, 2) if today_p is not None else None,
            "dist_20d_low": round(d_low, 2) if d_low is not None else None,
            "max_spike_5d": round(spike5, 2) if spike5 is not None else None,
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "layer": layer,
            "signals": signals,
            "priority": priority,
            "notes": notes,
            "warnings": warnings,
        }

    # Layer 分配规则:
    #   L2 = A ∩ D' ∩ 距低<25  -> layer 2 (主追踪池)
    #   L3 = 金子A 或 B∩D' 命中 但 不在 L2 中 -> layer 3 (独立 alert)
    #   L1 = A 命中 但 不在 L2 也不在 L3 中 -> layer 1 (粗筛池)
    l3_alert_mask = (gold_a_mask | bd_mask) & ~L2

    # 优先组装 L2 主池
    l2_codes = sorted([c for c in close_t.index if bool(L2.get(c, False))],
                      key=lambda c: -float(close_t[c]))
    for code in l2_codes:
        candidates.append(_make_row(code, layer=2))

    # Layer 3 alerts 独立成行 (不在 L2 但触发了 alert)
    l3_codes = sorted([c for c in close_t.index if bool(l3_alert_mask.get(c, False))],
                      key=lambda c: -float(close_t[c]))
    for code in l3_codes:
        candidates.append(_make_row(code, layer=3))

    # Layer 1 仅 A 命中 (粗筛池, 限量)
    layer1_max = config["layer1"]["max_output"]
    layer3_codes_set = set(l3_codes)
    l1_only_codes = sorted([c for c in close_t.index
                             if bool(L1.get(c, False))
                             and not bool(L2.get(c, False))
                             and c not in layer3_codes_set],
                            key=lambda c: -float(close_t[c]))
    for code in l1_only_codes[:layer1_max]:
        candidates.append(_make_row(code, layer=1))

    # 按 priority 排序: L2(layer=2) 在前 -> L3(layer=3) -> L1(layer=1)
    # 同 layer 内按 priority (1=最高) 排, 再按价格降
    layer_order = {2: 0, 3: 1, 1: 2}
    candidates.sort(key=lambda r: (layer_order.get(r["layer"], 9),
                                    r["priority"],
                                    -r["price"]))

    # 6. 写出
    summary = {
        "pool_size": pool_size,
        "layer1_count": int(L1.sum()),
        "layer2_count": int(L2.sum()),
        "layer3_count": int(l3_alert_mask.sum()),
        "gold_a_count": int(gold_a_mask.sum()),
        "bd_count": int(bd_mask.sum()),
        "d_baseline_count": int((mask_D_base & pool_mask).sum()),
        "candidates_output": len(candidates),
    }

    output = {
        "version": config["version"],
        "type": "p100_signals",
        "trade_date": today_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "min_price": min_price,
            "spike_range": [config["layer1"]["spike_low"], config["layer1"]["spike_high"]],
            "d_prime_z": [config["layer2"]["d_prime_z_low"], config["layer2"]["d_prime_z_high"]],
            "dist_low_max": config["layer2"]["dist_low_max"],
        },
        "summary": summary,
        "candidates": candidates,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    logger.info(f"✅ 候选输出 {OUTPUT_FILE}")
    logger.info(f"   summary: {summary}")
    if candidates:
        logger.info(f"   Top 5 候选:")
        for r in candidates[:5]:
            sig_str = "+".join(r["signals"])
            logger.info(f"     {r['name']}({r['code']}) 价{r['price']:.1f} "
                        f"今{r['today_pct']:+.2f}% 距低{r['dist_20d_low']:+.1f}% "
                        f"L{r['layer']} {sig_str}")

    # 7. 持有追踪 (配套机制)
    if config["holding"]["enabled"]:
        _update_holdings(panels, today_date, candidates, config["holding"])

    return True


# ==========================================================================
# 持有追踪 (配套机制)
# ==========================================================================

def _update_holdings(panels: dict, today_date: str, candidates: list[dict],
                     cfg: dict):
    """更新 p100_holding.json - 跟踪首次 L2_v2 命中的票"""
    # 加载历史 holdings
    holdings: dict = {}
    if HOLDING_FILE.exists():
        try:
            with open(HOLDING_FILE, encoding="utf-8") as f:
                d = json.load(f)
            holdings = d.get("positions", {})
        except Exception:
            pass

    close_t = panels["close"][today_date]

    # 添加今日新进 L2 票
    today_l2_codes = {r["code"] for r in candidates if r["layer"] == 2}
    for r in candidates:
        if r["layer"] != 2:
            continue
        code = r["code"]
        if code not in holdings:
            # 新进
            holdings[code] = {
                "code": code, "name": r["name"], "sector": r["sector"],
                "entry_date": today_date, "entry_price": r["price"],
                "entry_signals": r["signals"],
                "current_price": r["price"],
                "current_date": today_date,
                "max_close": r["price"], "max_close_date": today_date,
                "drawdown_from_peak": 0.0,
                "ret_from_entry": 0.0,
                "days_held": 0,
                "status": "active",
                "alerts": [],
            }

    # 更新已有持仓
    max_days = cfg["max_holding_days"]
    stop_dd = cfg["stop_loss_dd"]
    dates = panels["dates"]
    today_idx = dates.index(today_date)

    for code, h in list(holdings.items()):
        if h.get("status") == "exited":
            continue
        if code not in close_t.index:
            continue
        cur_p = float(close_t[code])
        if pd.isna(cur_p):
            continue
        # 更新当前
        h["current_price"] = round(cur_p, 2)
        h["current_date"] = today_date
        # 更新峰值
        if cur_p > h["max_close"]:
            h["max_close"] = round(cur_p, 2)
            h["max_close_date"] = today_date
        # 回撤
        h["drawdown_from_peak"] = round((cur_p - h["max_close"]) / h["max_close"] * 100, 2)
        # 入场起收益
        h["ret_from_entry"] = round((cur_p - h["entry_price"]) / h["entry_price"] * 100, 2)
        # 持有天数
        if h["entry_date"] in dates:
            h["days_held"] = today_idx - dates.index(h["entry_date"])

        # 警报
        alerts = []
        if h["drawdown_from_peak"] <= stop_dd:
            alerts.append(f"峰值回撤{h['drawdown_from_peak']:.1f}% < {stop_dd}% 止损考虑")
        if h["days_held"] >= max_days:
            alerts.append(f"持有{h['days_held']}天 ≥ {max_days}天 退出考虑")
            h["status"] = "exited"
        if h["ret_from_entry"] <= -10:
            alerts.append(f"入场亏{h['ret_from_entry']:.1f}% 超 -10%")
        h["alerts"] = alerts

    # 写出
    HOLDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "version": "1.0",
        "trade_date": today_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total": len(holdings),
            "active": sum(1 for h in holdings.values() if h.get("status") == "active"),
            "alerted": sum(1 for h in holdings.values() if h.get("alerts")),
        },
        "positions": holdings,
    }
    with open(HOLDING_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"   持仓追踪: total={out['summary']['total']} "
                f"active={out['summary']['active']} alerted={out['summary']['alerted']}")


# ==========================================================================
# CLI
# ==========================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="百元股趋势追踪器 v1.0")
    parser.add_argument("--mode", choices=["generate"], default="generate",
                        help="运行模式 (目前仅 generate)")
    args = parser.parse_args()

    _setup_logger()
    if args.mode == "generate":
        ok = generate_candidates()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
