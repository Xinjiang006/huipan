"""
慧盘 · 暗流盘中衍生指标采集器 v5.0
collector/derived_intraday.py

功能：读取 .spot_cache.pkl 计算盘中衍生指标，追加写入 derived_intraday.json
运行：python -m collector.derived_intraday [--slot 10:30]
调度：jobs.py 各采集点自动调用

采集时间点（与脉动一致）: 09:28 / 10:30 / 13:05 / 14:30 / 15:10
指标(25个):
  盘中实时·衍生(7): median_change_pct, change_pct_stdev, extreme_ratio,
               cap_scissors, volume_price_ratio, volume_concentration,
               high_price_count + high_price_avg_chg + high_price_up_count
  盘中实时·健康(7): zt_dt_ratio, up_ratio, up_count, down_count,
               limit_up, limit_down, vwap_bias_median, intraday_strength_median
  盘中实时·行情(5): volume_total, sh/sz/cyb/csi1000_change_pct
  开盘定值(1): zt_premium_avg (仅首个时间点计算，后续复用)
  盘中板块(5): realtime_sector_dist, realtime_hhi, realtime_top100_count,
               sector_delta, incubation_alerts

独立输出: sector_continuity.json（板块流量分类：续涨/新进/退出，1d/3d/5d）

数据源: 全部来自 .spot_cache.pkl + 指数行情（腾讯/Sina）+ sector_map.json + picks_history.json，零新增API

v5.0变更：
  - _load_picks_history 自动排除 date == today 的条目（时序保护，防止自比）
  - today_map 数量 < 100 时输出警告日志（排查37/100蒸发问题）
  - sector_continuity 输出加 date + today_count 字段（可追溯性）

v4.5变更：
  - 新增盘中实时板块分布（realtime_sector_dist / realtime_hhi）
  - 新增板块delta对比（和昨日收盘 sector_dist_gainers 比较）
  - 新增孵化预警（incubation_alerts: 孵化/加速/过热/退潮四级信号）

v4.3变更：
  - 指数扩展为4个（上证/深证/创业板/中证1000），存入每个snapshot
  - 新增涨跌停自算（zt_dt_ratio/limit_up/limit_down）
  - 新增涨跌家数（up_count/down_count/up_ratio）
  - 新增成交额（volume_total，亿元）
  - 新增updated_at时间戳
  - 修复regime_history.json指数/剪刀差长期null问题（配合regime_collector fallback）
"""

import sys
import os
import json
import pickle
import math
import time
import argparse
from pathlib import Path
from datetime import datetime, date

import requests
import pandas as pd
from loguru import logger
from sources.index import fetch_indices as _fetch_raw_indices
from utils.stock_filter import filter_valid as _sf_filter_valid, find_limit_up_codes, calc_limit_counts, calc_basic_counts

# ── 路径 ──
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

DATA_DIR = BASE_DIR / "static" / "data"
SPOT_CACHE = DATA_DIR / ".spot_cache.pkl"
OUTPUT_JSON = DATA_DIR / "derived_intraday.json"
CONTINUITY_JSON = DATA_DIR / "sector_continuity.json"
ARCHIVE_DIR = DATA_DIR / "archive" / "spot"

# 标准时间点（与脉动 intraday_tracker 一致）
STANDARD_SLOTS = ["09:28", "10:30", "13:05", "14:30", "15:10"]

# 腾讯指数代码
# sh000001=上证综指, sz399001=深证成指, sz399006=创业板指, sh000852=中证1000
INDEX_CODES_QQ = "sh000001,sz399001,sz399006,sh000852"

# 请求超时
HTTP_TIMEOUT = 10


# ═══════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════

def _load_spot(path: Path = None) -> pd.DataFrame | None:
    """加载 spot pkl，返回 DataFrame 或 None
    pkl结构: {"time": float, "df": DataFrame}
    DataFrame列: 代码/名称/最新价/涨跌额/涨跌幅/买入/卖出/昨收/今开/最高/最低/成交量/成交额/时间戳
    代码格式: sh600000 / sz000001 / bj830000
    """
    if path is None:
        path = SPOT_CACHE
    if not path.exists():
        logger.error(f"pkl不存在: {path}")
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        df = data["df"] if isinstance(data, dict) and "df" in data else data
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.error("pkl数据为空")
            return None
        logger.info(f"pkl加载: {len(df)}行, 来自{path.name}")
        return df
    except Exception as e:
        logger.error(f"pkl加载失败: {e}")
        return None


def _filter_valid(df: pd.DataFrame) -> pd.DataFrame:
    """过滤有效个股：v5.0统一使用stock_filter"""
    valid = _sf_filter_valid(df, exclude_bj=True, exclude_new=False, exclude_suspended=True)
    logger.info(f"有效个股: {len(valid)}只")
    return valid


# ═══════════════════════════════════════════
# 时间点
# ═══════════════════════════════════════════

def _nearest_slot(now_str: str = None) -> str:
    """找最近的标准时间点（≤当前时间的最后一个）"""
    if now_str is None:
        now_str = datetime.now().strftime("%H:%M")
    for slot in reversed(STANDARD_SLOTS):
        if now_str >= slot:
            return slot
    return STANDARD_SLOTS[0]


# ═══════════════════════════════════════════
# 指数数据（腾讯优先，Sina备选）
# ═══════════════════════════════════════════

def _fetch_index_changes() -> dict:
    """获取上证+深证+创业板+中证1000涨跌幅（通过Source层）
    返回: {"sh": float, "sz": float, "cyb": float, "csi1000": float}
    """
    CODES = ["sh000001", "sz399001", "sz399006", "sh000852"]
    KEY_MAP = {
        "sh000001": "sh",
        "sz399001": "sz",
        "sz399006": "cyb",
        "sh000852": "csi1000",
    }
    result = {"sh": None, "sz": None, "cyb": None, "csi1000": None}
    raw = _fetch_raw_indices(CODES)
    for k, v in raw.items():
        if k in KEY_MAP:
            result[KEY_MAP[k]] = v["change_pct"]
    filled = sum(1 for v in result.values() if v is not None)
    if filled > 0:
        logger.info(f"指数(Source层): {filled}/4")
    return result



# ═══════════════════════════════════════════
# 涨停溢价率（开盘定值）
# ═══════════════════════════════════════════

def _find_yesterday_pkl() -> Path | None:
    """找最近一个交易日的归档pkl（严格早于今天）"""
    if not ARCHIVE_DIR.exists():
        return None
    today_str = date.today().strftime("%Y%m%d")
    pkls = sorted(ARCHIVE_DIR.glob("spot_*.pkl"), reverse=True)
    for p in pkls:
        pkl_date = p.stem.replace("spot_", "")
        if pkl_date < today_str:
            return p
    return None


def _calc_zt_premium(today_df: pd.DataFrame) -> float | None:
    """计算涨停次日溢价率
    v5.0: 使用stock_filter统一判定涨停（区分板制阈值），替代硬编码9.8%
    """
    yd_path = _find_yesterday_pkl()
    if yd_path is None:
        logger.info("无昨日pkl，跳过溢价率")
        return None

    try:
        yd_df = _load_spot(yd_path)
        if yd_df is None:
            return None

        # v5.0: 通过stock_filter统一判定昨日涨停股（自动处理创业板20%/科创板20%/ST 5%等）
        zt_codes = find_limit_up_codes(yd_df, exclude_bj=True)
        if not zt_codes:
            logger.info("昨日无涨停股")
            return 0.0

        # 转换为带前缀格式匹配today_df
        logger.info(f"昨日涨停: {len(zt_codes)}只")

        # 今日数据中匹配（today_df的代码带前缀，zt_codes是6位纯数字）
        today_codes_stripped = today_df["代码"].astype(str).str.replace(r"^[a-z]{2}", "", regex=True)
        mask = today_codes_stripped.isin(zt_codes) & (today_df["今开"] > 0) & (today_df["昨收"] > 0)
        matched = today_df[mask].copy()
        if matched.empty:
            return None

        matched["premium"] = (matched["今开"] - matched["昨收"]) / matched["昨收"] * 100
        avg = round(matched["premium"].mean(), 2)
        logger.info(f"涨停溢价率: {avg:+.2f}% ({len(matched)}只匹配)")
        return avg
    except Exception as e:
        logger.warning(f"溢价率计算异常: {e}")
        return None


# ═══════════════════════════════════════════
# 板块数据加载
# ═══════════════════════════════════════════

def _load_sector_map() -> dict:
    """加载 sector_map.json，返回 {代码(不含前缀): sector} 映射
    sector_map.json 结构: {"map": {"sh600000": "银行", ...}}
    """
    sector_map_path = BASE_DIR / "config" / "sector_map.json"
    if not sector_map_path.exists():
        logger.warning("sector_map.json 不存在，跳过板块计算")
        return {}
    try:
        with open(sector_map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = data.get("map", {})
        logger.info(f"sector_map 加载: {len(mapping)}只")
        return mapping
    except Exception as e:
        logger.warning(f"sector_map 加载失败: {e}")
        return {}


def _load_yesterday_sector_dist() -> dict:
    """从 regime_history.json 读取昨日收盘的 sector_dist_gainers，作为 delta 基准

    v5.8 fix: 跳过 date == today 的盘中record，取第一条 date != today 的作为"昨日"。
    盘中 regime_collector 会写入今天的record到 history[0]，导致 delta 全0。

    返回: {"医药生物": 12, ...} 或 {}
    """
    regime_path = DATA_DIR / "regime_history.json"
    if not regime_path.exists():
        return {}
    try:
        with open(regime_path, "r", encoding="utf-8") as f:
            history = json.load(f)
        if not history:
            return {}
        # 跳过今天的盘中record
        today_str = date.today().isoformat()
        for record in history:
            record_date = str(record.get("date", ""))[:10]
            if record_date == today_str:
                continue
            dist = record.get("sector_dist_gainers")
            if dist and isinstance(dist, dict):
                logger.info(f"昨日板块基准: date={record_date}, {len(dist)}个板块")
                return dist
        return {}
    except Exception as e:
        logger.warning(f"昨日板块分布读取失败: {e}")
        return {}


def _load_regime_sector_history(n_days=5) -> list:
    """加载最近N天的 sector_dist_gainers（T-1..T-N），排除今天盘中record
    返回: [{sector: count}, ...] 最新在 index 0，用于 phase_day 连续天数计算
    """
    regime_path = DATA_DIR / "regime_history.json"
    if not regime_path.exists():
        return []
    try:
        with open(regime_path, "r", encoding="utf-8") as f:
            history = json.load(f)
        today_str = date.today().isoformat()
        result = []
        for entry in history:
            entry_date = str(entry.get("date", ""))[:10]
            if entry_date == today_str:
                continue  # 跳过今天的盘中record
            dist = entry.get("sector_dist_gainers")
            result.append(dist if isinstance(dist, dict) else {})
            if len(result) >= n_days:
                break
        return result
    except Exception:
        return []


def _calc_realtime_sectors(
    df: pd.DataFrame,
    sector_map: dict,
    yd_dist: dict,
) -> dict:
    """计算实时 Top100 板块分布、HHI、delta、孵化预警

    参数:
        df: 有效个股 DataFrame（已排除北交所）
        sector_map: {代码(含前缀 sh/sz): sector}
        yd_dist: 昨日收盘 sector_dist_gainers {sector: count}

    返回: dict，含 realtime_sector_dist / realtime_hhi /
                  realtime_top100_count / sector_delta / incubation_alerts
    """
    # Top100 纯涨幅排序（与 regime_collector 口径一致）
    top100 = df.nlargest(100, "涨跌幅")
    total = len(top100)

    if total == 0 or not sector_map:
        return {
            "realtime_sector_dist": {},
            "realtime_sector_stocks": {},
            "realtime_hhi": None,
            "realtime_top100_count": 0,
            "sector_delta": {},
            "incubation_alerts": [],
        }

    # 板块计数 + 实时个股列表
    sector_counts: dict[str, int] = {}
    sector_stocks: dict[str, list] = {}
    for _, row in top100.iterrows():
        code = str(row["代码"])
        sector = sector_map.get(code[2:], "其他")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        sector_stocks.setdefault(sector, []).append({
            "code": code[2:],
            "name": str(row["名称"]),
            "change_pct": round(float(row["涨跌幅"]), 2),
            "price": round(float(row["最新价"]), 2),
        })

    # HHI（赫芬达尔指数）
    hhi = round(sum((c / total * 100) ** 2 for c in sector_counts.values()))

    # Delta：今日 vs 昨日
    all_sectors = set(sector_counts.keys()) | set(yd_dist.keys())
    sector_delta = {}
    for s in all_sectors:
        today_cnt = sector_counts.get(s, 0)
        yd_cnt = yd_dist.get(s, 0)
        delta = today_cnt - yd_cnt
        if today_cnt > 0 or yd_cnt > 0:
            sector_delta[s] = {
                "today": today_cnt,
                "yesterday": yd_cnt,
                "delta": delta,
            }

    # 孵化预警（四级信号）
    alerts = []
    for sector, today_cnt in sector_counts.items():
        yd_cnt = yd_dist.get(sector, 0)
        pct = today_cnt / total * 100

        signals = []
        if today_cnt > 5 and yd_cnt <= 5:
            signals.append("孵化")
        if today_cnt > 15:
            signals.append("加速")
        if pct > 25:
            signals.append("过热")

        for sector2, yd_cnt2 in yd_dist.items():
            if sector2 == sector and yd_cnt2 > 15 and today_cnt < 10:
                signals.append("退潮")

        if signals:
            alerts.append({
                "sector": sector,
                "count": today_cnt,
                "yesterday": yd_cnt,
                "delta": today_cnt - yd_cnt,
                "pct": round(pct, 1),
                "signals": signals,
            })

    # HHI过热：整体指标，只标记占比最高的板块
    if hhi > 1300 and alerts:
        top_alert = max(alerts, key=lambda a: a["count"])
        if "过热" not in top_alert["signals"]:
            top_alert["signals"].insert(0, "过热")
    # alerts 按信号优先级排序（过热>加速>孵化>退潮）
    _priority = {"过热": 0, "加速": 1, "孵化": 2, "退潮": 3}
    alerts.sort(key=lambda a: min(_priority.get(s, 9) for s in a["signals"]))

    logger.info(
        f"实时板块: Top100共{total}只, HHI={hhi}, "
        f"板块数={len(sector_counts)}, 预警={len(alerts)}条"
    )
    if alerts:
        for a in alerts[:3]:
            logger.info(f"  ⚠️ {a['sector']} {a['count']}只 "
                        f"(昨{a['yesterday']}只 Δ{a['delta']:+d}) {a['signals']}")

    return {
        "realtime_sector_dist": sector_counts,
        "realtime_sector_stocks": sector_stocks,
        "realtime_hhi": hhi,
        "realtime_top100_count": total,
        "sector_delta": sector_delta,
        "incubation_alerts": alerts,
    }


# ═══════════════════════════════════════════
# v5.7 板块生命周期标签（sector_phases）
# ═══════════════════════════════════════════

def _determine_phase(today_cnt: int, total: int, yd_cnt: int) -> str | None:
    """确定板块生命周期阶段（单一标签，优先级高→低）

    条件说明:
        过热: 占比>25% — 板块独大，次日反转概率高
        退潮: 昨日>15 今日<10 — 资金撤离
        加速: 今日>15 — 资金涌入
        孵化: 今日>5 且昨日≤5 — 从无到有的早期信号
        活跃: 今日≥3 — 有板块效应但无明显趋势变化
    """
    if today_cnt < 3:
        return None
    pct = today_cnt / total * 100 if total > 0 else 0
    if pct > 25:
        return "过热"
    if yd_cnt > 15 and today_cnt < 10:
        return "退潮"
    if today_cnt > 15:
        return "加速"
    if today_cnt > 5 and yd_cnt <= 5:
        return "孵化"
    return "活跃"


def _calc_sector_phases(
    sector_counts: dict,
    total: int,
    yd_dist: dict,
    history_dists: list,
) -> dict:
    """计算所有≥3只板块的生命周期标签 + 连续天数

    Args:
        sector_counts: 今日 {sector: count}
        total: Top100 总数（通常100）
        yd_dist: 昨日 {sector: count}（= history_dists[0]）
        history_dists: [T-1, T-2, T-3, ...] 每个 {sector: count}

    Returns:
        {sector: {"phase": str, "phase_day": int, "count": int, "yesterday": int}}
    """
    phases = {}

    for sector, today_cnt in sector_counts.items():
        if today_cnt < 3:
            continue

        yd_cnt = yd_dist.get(sector, 0)
        phase = _determine_phase(today_cnt, total, yd_cnt)
        if phase is None:
            continue

        # 连续天数：从 T-1 往回走，看历史上每一天是否也落在同一 phase
        phase_day = 1
        for i in range(len(history_dists)):
            h_cnt = history_dists[i].get(sector, 0)
            h_yd_cnt = history_dists[i + 1].get(sector, 0) if i + 1 < len(history_dists) else 0
            h_phase = _determine_phase(h_cnt, 100, h_yd_cnt)
            if h_phase == phase:
                phase_day += 1
            else:
                break

        phases[sector] = {
            "phase": phase,
            "phase_day": phase_day,
            "count": today_cnt,
            "yesterday": yd_cnt,
        }

    if phases:
        top3 = sorted(phases.items(), key=lambda x: x[1]["count"], reverse=True)[:3]
        logger.info("板块Phase: " + " | ".join(
            f"{s} {d['phase']}第{d['phase_day']}天({d['count']}只)" for s, d in top3
        ))

    return phases


# ═══════════════════════════════════════════
# v4.6 Top100 资金流向分类（sector_continuity）
# ═══════════════════════════════════════════

def _count_cont_days(code: str, picks_history: list) -> int:
    """统计某股票从最近一天起连续出现在 Top100 gainers 的天数

    Args:
        code: 6位股票代码（不含前缀）
        picks_history: picks_history 数据，index 0 = 最新

    Returns:
        连续天数（0 = 历史中从未出现）
    """
    days = 0
    for day in picks_history:
        codes_in_day = {s.get("code", "") for s in day.get("top100_gainers", [])}
        if code in codes_in_day:
            days += 1
        else:
            break
    return days

def _load_picks_history() -> list:
    """加载 picks_history.json（5天滚动），返回 list[dict] 或 []
    每条: {date, top100_gainers: [{code, name, change_pct, sector, ...}], top100_losers: [...]}
    index 0 = 最新（昨日）

    v5.8: 自动排除 date == today 的条目，防止 _save_deferred_picks 写入今天后
          sector_continuity 变成自己 vs 自己（时序保护）
    """
    path = DATA_DIR / "picks_history.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
        # 兼容 dict{date: {top100_gainers, ...}} 和 list[{date, ...}] 两种格式
        if isinstance(history, dict):
            history = [{'date': k, **v} for k, v in sorted(history.items(), reverse=True)]
        if not isinstance(history, list):
            return []
        # ── 时序保护：排除今天的条目 ──
        today_str = date.today().isoformat()
        before = len(history)
        history = [h for h in history if h.get("date") != today_str]
        if len(history) < before:
            logger.info(f"  picks_history: 排除今日({today_str})条目，{before}→{len(history)}条")
        return history
    except Exception as e:
        logger.warning(f"picks_history 加载失败: {e}")
        return []


def _calc_sector_continuity(
    df: pd.DataFrame,
    sector_map: dict,
    picks_history: list,
    raw_df: pd.DataFrame = None,
) -> dict | None:
    """计算板块续涨/新进/退出流量，输出 sector_continuity.json 结构

    对 1d/3d/5d 三个周期，分板块统计：
    - cont: 上期也在 Top100（续涨）
    - new_in: 上期不在 Top100（新进），标注 from_loser
    - dropped: 上期在但今日不在（退出）

    参数:
        df: 有效个股 DataFrame（已排除北交所）
        sector_map: {6位代码: sector}
        picks_history: picks_history.json 数据

    返回: dict (sector_continuity.json 结构) 或 None
    """
    if df.empty or not sector_map or not picks_history:
        return None

    # 今日 Top100（与 _calc_realtime_sectors 口径一致）
    top100 = df.nlargest(100, "涨跌幅")

    # 构建今日 Top100 详情 {code: {name, change_pct, sector}}
    today_map = {}
    for _, row in top100.iterrows():
        code_full = str(row["代码"])
        code = code_full[2:]  # 去 sh/sz/bj 前缀
        today_map[code] = {
            "code": code,
            "name": str(row["名称"]),
            "change_pct": round(float(row["涨跌幅"]), 2),
            "sector": sector_map.get(code, "其他"),
        }

    today_codes = set(today_map.keys())

    # v5.8: 映射数量监控（排查37/100蒸发问题）
    if len(today_map) < 100:
        logger.warning(
            f"  ⚠️ today_map只有{len(today_map)}/100只"
            f"（top100行数={len(top100)}，可能有重复代码或前缀问题）"
        )
        # 打印前5个重复检测
        seen_codes = {}
        for _, row in top100.iterrows():
            code_full = str(row["代码"])
            code = code_full[2:]
            if code in seen_codes:
                logger.warning(f"    重复代码: {code} ← {code_full} vs {seen_codes[code]}")
            else:
                seen_codes[code] = code_full

    # 全市场 code→涨跌幅 dict，供 dropped_stocks 查今日涨跌幅
    # 用 raw_df（未过滤）确保停牌/北交所退出股票也能查到
    lookup_df = raw_df if raw_df is not None else df
    all_market_pct = {}
    for _, row in lookup_df.iterrows():
        c6 = str(row["代码"])[2:]  # 去 sh/sz 前缀
        try:
            all_market_pct[c6] = round(float(row["涨跌幅"]), 2)
        except (ValueError, TypeError):
            pass

    result = {
        "date": date.today().isoformat(),
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "today_count": len(today_map),
    }

    for period, n_days in [("1d", 1), ("3d", 3), ("5d", 5)]:
        if len(picks_history) < n_days:
            continue

        # 参考期：过去 N 天的 Top100 gainers / losers
        ref_gainers = {}   # {code: {name, change_pct, sector, ref_date}}
        ref_losers = set()
        ref_date = picks_history[0].get("date", "")

        for i in range(min(n_days, len(picks_history))):
            day = picks_history[i]
            d = day.get("date", "")
            for s in day.get("top100_gainers", []):
                c = s.get("code", "")
                if c and c not in ref_gainers:  # 就近优先：index 0 先写入
                    ref_gainers[c] = {
                        "name": s.get("name", ""),
                        "change_pct": s.get("change_pct", 0),
                        "sector": s.get("sector", "其他"),
                        "ref_date": d,
                    }
            for s in day.get("top100_losers", []):
                c = s.get("code", "")
                if c:
                    ref_losers.add(c)

        ref_codes = set(ref_gainers.keys())

        # 全局分类
        cont_codes = today_codes & ref_codes       # 续涨
        new_codes = today_codes - ref_codes         # 新进
        dropped_codes = ref_codes - today_codes     # 退出

        # 按板块聚合
        all_sectors = set()
        for c in today_codes:
            all_sectors.add(today_map[c]["sector"])
        for c in ref_codes:
            all_sectors.add(ref_gainers[c]["sector"])

        sectors = {}
        for sec in sorted(all_sectors):
            # 该板块的今日和参考期代码
            sec_today = {c for c in today_codes if today_map[c]["sector"] == sec}
            sec_ref = {c for c in ref_codes if ref_gainers[c]["sector"] == sec}

            sec_cont = sec_today & sec_ref
            sec_new = sec_today - sec_ref
            sec_drop = sec_ref - sec_today

            if not sec_today and not sec_ref:
                continue

            # 构建个股明细列表
            cont_stocks = []
            for c in sorted(sec_cont, key=lambda x: today_map[x]["change_pct"], reverse=True):
                cont_stocks.append({
                    "code": c,
                    "name": today_map[c]["name"],
                    "today_pct": today_map[c]["change_pct"],
                    "ref_pct": ref_gainers[c]["change_pct"],
                })

            new_stocks = []
            for c in sorted(sec_new, key=lambda x: today_map[x]["change_pct"], reverse=True):
                new_stocks.append({
                    "code": c,
                    "name": today_map[c]["name"],
                    "today_pct": today_map[c]["change_pct"],
                    "ref_pct": None,
                    "from_loser": c in ref_losers,
                })

            dropped_stocks = []
            for c in sorted(sec_drop, key=lambda x: ref_gainers[x]["change_pct"], reverse=True):
                today_pct = all_market_pct.get(c)
                dropped_stocks.append({
                    "code": c,
                    "name": ref_gainers[c]["name"],
                    "today_pct": today_pct,
                    "ref_pct": ref_gainers[c]["change_pct"],
                })

            sectors[sec] = {
                "ref": len(sec_ref),
                "today": len(sec_today),
                "cont": len(sec_cont),
                "new_in": len(sec_new),
                "dropped": len(sec_drop),
                "delta": len(sec_today) - len(sec_ref),
                "cont_stocks": cont_stocks,
                "new_stocks": new_stocks,
                "dropped_stocks": dropped_stocks,
            }

            # v5.7 龙头标记：续涨股中连续天数最多 + 涨幅最大
            if cont_stocks:
                leader_candidates = [
                    (s, _count_cont_days(s["code"], picks_history))
                    for s in cont_stocks
                ]
                best = max(leader_candidates, key=lambda x: (x[1], x[0]["today_pct"]))
                stock, cont_d = best
                # 涨停判定
                limit_pct = 20.0 if stock["code"].startswith(("300", "688")) else 10.0
                pct = stock["today_pct"]
                if pct >= limit_pct - 0.5:
                    status = "涨停"
                elif pct >= 5:
                    status = "强势"
                elif pct > 0:
                    status = "上涨"
                elif pct > -3:
                    status = "震荡"
                else:
                    status = "走弱"
                sectors[sec]["leader"] = {
                    "code": stock["code"],
                    "name": stock["name"],
                    "today_pct": pct,
                    "cont_days": cont_d,
                    "status": status,
                }

        result[period] = {
            "ref_date": ref_date,
            "total_cont": len(cont_codes),
            "total_new": len(new_codes),
            "total_dropped": len(dropped_codes),
            "sectors": sectors,
        }

    if len(result) <= 1:  # 只有 updated_at，无任何 period
        return None

    logger.info(
        f"板块流量: "
        + ", ".join(
            f"{p}期 续{result[p]['total_cont']}/新{result[p]['total_new']}/退{result[p]['total_dropped']}"
            for p in ["1d", "3d", "5d"] if p in result
        )
    )

    return result


# ═══════════════════════════════════════════
# 核心计算
# ═══════════════════════════════════════════

def calc_derived(df: pd.DataFrame, slot: str) -> dict:
    """计算全部盘中衍生指标（不含zt_premium_avg，由调用方处理）

    参数:
        df: 过滤后的有效个股DataFrame
        slot: 时间点标签

    返回: dict，字段与 regime_daily 对应
    """
    result = {"time": slot}

    # ── 1. 赚钱效应·中位数 ──
    result["median_change_pct"] = round(df["涨跌幅"].median(), 2)

    # ── 2. 涨跌幅标准差 ──
    result["change_pct_stdev"] = round(df["涨跌幅"].std(), 2)

    # ── 3. 极端涨跌比 ──
    up_extreme = int((df["涨跌幅"] > 5).sum())
    down_extreme = int((df["涨跌幅"] < -5).sum())
    result["extreme_ratio"] = round(up_extreme / max(down_extreme, 1), 2)

    # ── 4. 大小盘剪刀差 ──
    indices = _fetch_index_changes()
    if indices["sh"] is not None and indices["csi1000"] is not None:
        result["cap_scissors"] = round(indices["sh"] - indices["csi1000"], 2)
    else:
        result["cap_scissors"] = None

    # ── 5. 量价配合度 ──
    top100_up = df.nlargest(100, "涨跌幅")
    top100_down = df.nsmallest(100, "涨跌幅")
    vol_up_avg = top100_up["成交额"].mean()
    vol_down_avg = top100_down["成交额"].mean()
    if vol_down_avg and vol_down_avg > 0:
        result["volume_price_ratio"] = round(vol_up_avg / vol_down_avg, 2)
    else:
        result["volume_price_ratio"] = None

    # ── 6. 成交额集中度 ──
    total_vol = df["成交额"].sum()
    if total_vol > 0:
        top10_vol = df.nlargest(10, "成交额")["成交额"].sum()
        result["volume_concentration"] = round(top10_vol / total_vol * 100, 2)
    else:
        result["volume_concentration"] = None

    # ── 7. 高价股统计 ──
    high_price = df[df["最新价"] > 100]
    result["high_price_count"] = int(len(high_price))
    if len(high_price) > 0:
        result["high_price_avg_chg"] = round(high_price["涨跌幅"].mean(), 2)
        result["high_price_up_count"] = int((high_price["涨跌幅"] > 0).sum())
    else:
        result["high_price_avg_chg"] = 0.0
        result["high_price_up_count"] = 0

    # ── 8. 涨跌停自算（v5.0: 统一使用stock_filter）──
    lu, ld = calc_limit_counts(df)
    result["limit_up"] = lu
    result["limit_down"] = ld

    # ── 9. 涨跌停比（保留计算，不再作为健康卡片展示）──
    if ld > 0:
        result["zt_dt_ratio"] = round(lu / ld, 2)
    elif lu > 0:
        result["zt_dt_ratio"] = float(lu)
    else:
        result["zt_dt_ratio"] = None

    # ── 9a. VWAP偏离中位数（v5.3: 替换涨停/跌停比卡片）──
    try:
        amount = df["成交额"].astype(float)
        volume = df["成交量"].astype(float)
        close = df["最新价"].astype(float)
        mask = (volume > 0) & (amount > 0)
        if mask.sum() > 0:
            vwap = amount[mask] / volume[mask]
            vwap_mask = vwap > 0
            if vwap_mask.sum() > 0:
                bias = (close[mask][vwap_mask] - vwap[vwap_mask]) / vwap[vwap_mask] * 100
                result["vwap_bias_median"] = round(float(bias.median()), 2)
            else:
                result["vwap_bias_median"] = None
        else:
            result["vwap_bias_median"] = None
    except Exception as e:
        logger.warning(f"VWAP偏离计算异常: {e}")
        result["vwap_bias_median"] = None

    # ── 9b. 日内强度中位数（v5.3: 替换新高-新低差卡片）──
    try:
        high = df["最高"].astype(float)
        low = df["最低"].astype(float)
        close_s = df["最新价"].astype(float)
        amp = high - low
        amp_mask = amp > 0
        if amp_mask.sum() > 0:
            strength = (close_s[amp_mask] - low[amp_mask]) / amp[amp_mask] * 100
            result["intraday_strength_median"] = round(float(strength.median()), 2)
        else:
            result["intraday_strength_median"] = None
    except Exception as e:
        logger.warning(f"日内强度计算异常: {e}")
        result["intraday_strength_median"] = None

    # ── 10. 涨跌家数 + 涨跌比 ──
    basics = calc_basic_counts(df)
    result["up_count"] = basics["up_count"]
    result["down_count"] = basics["down_count"]
    result["up_ratio"] = basics["up_ratio"]

    # ── 11. 成交额（亿元）──
    vol_total = df["成交额"].sum()
    result["volume_total"] = round(vol_total / 1e8, 2) if vol_total else None

    # ── 12. 指数涨跌幅（存入snapshot供前端和regime_collector使用）──
    result["sh_change_pct"] = indices.get("sh")
    result["sz_change_pct"] = indices.get("sz")
    result["cyb_change_pct"] = indices.get("cyb")
    result["csi1000_change_pct"] = indices.get("csi1000")

    # ── 13. 更新时间 ──
    result["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # ── 14. 实时板块分布（v4.5）──
    sector_map = _load_sector_map()
    yd_dist = _load_yesterday_sector_dist()
    sector_result = _calc_realtime_sectors(df, sector_map, yd_dist)
    result.update(sector_result)

    # ── 15. 板块生命周期标签（v5.7）──
    history_dists = _load_regime_sector_history(5)
    result["sector_phases"] = _calc_sector_phases(
        sector_result.get("realtime_sector_dist", {}),
        sector_result.get("realtime_top100_count", 100),
        yd_dist,
        history_dists,
    )

    return result


# ═══════════════════════════════════════════
# JSON 工具
# ═══════════════════════════════════════════

def _clean_nan(obj):
    """JSON序列化前清理NaN/Inf → None"""
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _atomic_write_json(path: Path, data: dict):
    """原子写入JSON：先写.tmp再rename"""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))


# ═══════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════

def collect_derived_intraday(slot: str = None):
    """主函数：计算盘中衍生指标并追加写入JSON

    参数:
        slot: 指定时间点（如 "10:30"），None时自动判断
    """
    logger.info("═══ 暗流盘中指标 ═══")

    # 1. 加载spot
    raw_df = _load_spot()
    if raw_df is None:
        return False
    df = _filter_valid(raw_df)
    if df.empty:
        logger.error("无有效个股数据")
        return False

    # 2. 确定时间点
    if slot is None:
        slot = _nearest_slot()
    logger.info(f"时间点: {slot}")

    # 3. 计算7个实时指标
    snapshot = calc_derived(df, slot)

    # 4. 读现有JSON（跨日清空）
    today_str = date.today().isoformat()
    existing = {"date": today_str, "snapshots": []}
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("date") != today_str:
                logger.info(f"跨日清空（{existing.get('date')} → {today_str}）")
                existing = {"date": today_str, "snapshots": []}
        except Exception:
            existing = {"date": today_str, "snapshots": []}

    # 5. 涨停溢价率：首个时间点计算，后续复用
    if not existing["snapshots"]:
        # 当天第一个点，需要计算
        snapshot["zt_premium_avg"] = _calc_zt_premium(raw_df)
    else:
        # 复用当天第一个snapshot的值
        snapshot["zt_premium_avg"] = existing["snapshots"][0].get("zt_premium_avg")

    # 6. 追加/覆盖同slot
    existing["snapshots"] = [s for s in existing["snapshots"] if s["time"] != slot]
    existing["snapshots"].append(snapshot)
    existing["snapshots"].sort(key=lambda s: s["time"])

    # 7. 原子写入
    existing = _clean_nan(existing)
    _atomic_write_json(OUTPUT_JSON, existing)

    logger.info(f"✅ derived_intraday.json 已更新 ({len(existing['snapshots'])}个时间点)")

    # 8. 板块流量分析（v4.6 sector_continuity.json）
    try:
        picks_history = _load_picks_history()
        if picks_history:
            continuity = _calc_sector_continuity(df, _load_sector_map(), picks_history, raw_df=raw_df)
            if continuity:
                continuity = _clean_nan(continuity)
                _atomic_write_json(CONTINUITY_JSON, continuity)
                logger.info(f"✅ sector_continuity.json 已更新")
            else:
                logger.info("  ℹ️ sector_continuity 数据不足，跳过")
        else:
            logger.info("  ℹ️ picks_history.json 不存在，跳过 sector_continuity")
    except Exception as e:
        logger.warning(f"  ⚠️ sector_continuity 计算失败（不影响主流程）: {e}")

    # 打印摘要
    logger.info(
        f"   中位数{snapshot.get('median_change_pct', '?'):+}% "
        f"| 标准差{snapshot.get('change_pct_stdev', '?')}% "
        f"| 极端比{snapshot.get('extreme_ratio', '?')}x "
        f"| 剪刀差{snapshot.get('cap_scissors', '?')}% "
        f"| 量价{snapshot.get('volume_price_ratio', '?')}x "
        f"| 集中度{snapshot.get('volume_concentration', '?')}% "
        f"| 高价股{snapshot.get('high_price_count', '?')}只"
    )
    logger.info(
        f"   涨停{snapshot.get('limit_up', '?')}/跌停{snapshot.get('limit_down', '?')} "
        f"| VWAP偏离{snapshot.get('vwap_bias_median', '?')}% "
        f"| 日内强度{snapshot.get('intraday_strength_median', '?')}% "
        f"| 涨{snapshot.get('up_count', '?')}/跌{snapshot.get('down_count', '?')} "
        f"| 成交额{snapshot.get('volume_total', '?')}亿"
    )
    return True


# ═══════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="暗流盘中衍生指标采集 · python -m collector.derived_intraday"
    )
    parser.add_argument(
        "--slot",
        choices=STANDARD_SLOTS,
        help="指定时间点（默认自动判断）",
    )
    args = parser.parse_args()

    ok = collect_derived_intraday(slot=args.slot)
    sys.exit(0 if ok else 1)
