"""
慧盘 · 反转追踪器 v5.7
collector/reversal_tracker.py

功能：从跌幅Top100中筛选"止跌反转"候选票，6维打分+盘中监控
运行：python -m collector.reversal_tracker --mode generate|monitor [--slot 10:30]
调度：
  - 15:10  mode=generate  （regime_collector 之后，候选生成）
  - 10:30  mode=monitor   （derived_intraday 之后，盘中监控）
  - 14:30  mode=monitor   （同上）

两阶段工作流：
  Phase 1 · T日15:10 候选生成：
    picks_history(5天losers) + 今日spot + 归档pkl → 止跌判定 → 6维打分
    → reversal_watchlist.json

  Phase 2 · T+1盘中 10:30/14:30 监控：
    reversal_watchlist.json + 今日spot → 名单票表现 + 板块共振
    → reversal_monitor.json

6维打分（总分100）：
  ① 止跌天数(20)  ② 成交额比(15)  ③ 板块孵化(20)
  ④ 板块密度(15)   ⑤ 催化剂(15)    ⑥ 跌幅深度(15)

v5.6 改进（自选股系统增强）：
  - #2 催化剂自动识别：板块in_top100>=15+加速→10分，>=10+孵化→5分
  - #3 量比分层：>=1.5 primary / >=1.0 secondary / <1.0 weak
  - #4 涨停标记：triggered_today 字段，防误追涨停板
  - #5 三因子交叉验证：Top100×由弱转强×#3板块，triple_factor_picks 独立输出

v5.7 改进（基础设施迁移）：
  - pkl 加载/查询/过滤 迁移至 utils/pkl_helper.py（消除重复代码）
  - 日志配置 迁移至 utils/log_helper.py（动态级别切换）
  - 打分逻辑不变（E 阶段再改）

数据源：全部来自现有文件，零新增API
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

import pandas as pd
from loguru import logger

# ── 路径 ──
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

DATA_DIR = BASE_DIR / "static" / "data"
PICKS_HISTORY = DATA_DIR / "picks_history.json"
DERIVED_INTRADAY = DATA_DIR / "derived_intraday.json"
WATCHLIST_STATUS = DATA_DIR / "watchlist_status.json"
SECTOR_MAP_PATH = BASE_DIR / "config" / "sector_map.json"
CONFIG_PATH = BASE_DIR / "config" / "reversal_config.json"

OUTPUT_WATCHLIST = DATA_DIR / "reversal_watchlist.json"
OUTPUT_MONITOR = DATA_DIR / "reversal_monitor.json"

# ── utils 导入 ──
from utils.log_helper import setup_logger
from utils.pkl_helper import (
    load_spot,
    filter_valid,
    lookup_stock,
    preload_archives,
    FIELD_CODE,
    FIELD_PCT,
    FIELD_AMOUNT,
)

# 标准时间点
STANDARD_SLOTS = ["09:28", "10:30", "13:05", "14:30", "15:10"]

# ═══════════════════════════════════════════
# 默认配置
# ═══════════════════════════════════════════

DEFAULT_CONFIG = {
    # 止跌判定
    "stop_threshold": -2.0,       # 当日涨跌幅 > 此值 → 视为止跌
    "rebounce_reset": -5.0,       # 中间某天跌幅 < 此值 → 重置止跌计数
    # 候选筛选
    "lookback_days": 5,           # 往回看几天的 losers
    "lookback_mode": "daily",     # "daily"=每天独立top100去重, "cumulative"=N天累计(预留)
    "min_score": 30,              # 最低分数门槛，低于此分不输出
    "max_candidates": 30,         # 最多输出候选数
    # 6维权重
    "weights": {
        "stop_days": 20,
        "volume_ratio": 15,
        "sector_incubation": 20,
        "sector_density": 15,
        "catalyst": 15,
        "drop_depth": 15,
    },
}


def _load_config() -> dict:
    """加载配置文件，不存在则用默认值"""
    config = DEFAULT_CONFIG.copy()
    config["weights"] = DEFAULT_CONFIG["weights"].copy()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            # 合并（浅层）
            for k, v in user_cfg.items():
                if k == "weights" and isinstance(v, dict):
                    config["weights"].update(v)
                else:
                    config[k] = v
            logger.info(f"配置加载: {CONFIG_PATH.name}")
        except Exception as e:
            logger.warning(f"配置加载失败，用默认值: {e}")
    else:
        logger.info("无自定义配置，用默认值")
    return config


# ═══════════════════════════════════════════
# 数据加载工具（业务层，非 pkl 通用逻辑）
# ═══════════════════════════════════════════

def _load_picks_history() -> list:
    """加载 picks_history.json，返回 list[dict]
    每条: {date, top100_gainers: [...], top100_losers: [...]}
    index 0 = 最新
    """
    if not PICKS_HISTORY.exists():
        logger.warning("picks_history.json 不存在")
        return []
    try:
        with open(PICKS_HISTORY, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [{"date": k, **v} for k, v in sorted(data.items(), reverse=True)]
        return []
    except Exception as e:
        logger.warning(f"picks_history 加载失败: {e}")
        return []


def _load_sector_map() -> dict:
    """加载 sector_map.json → {6位代码: sector}
    sector_map.json 结构: {"map": {"sh600000": "银行", ...}}
    返回去掉前缀的映射: {"600000": "银行", ...}
    """
    if not SECTOR_MAP_PATH.exists():
        return {}
    try:
        with open(SECTOR_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("map", {})
        # 去前缀
        return {k[2:] if len(k) > 6 else k: v for k, v in raw.items()}
    except Exception as e:
        logger.warning(f"sector_map 加载失败: {e}")
        return {}


def _load_derived_intraday() -> dict | None:
    """加载 derived_intraday.json → 最新 snapshot"""
    if not DERIVED_INTRADAY.exists():
        return None
    try:
        with open(DERIVED_INTRADAY, "r", encoding="utf-8") as f:
            data = json.load(f)
        snapshots = data.get("snapshots", [])
        if not snapshots:
            return None
        return snapshots[-1]  # 最新时间点
    except Exception:
        return None


def _load_watchlist_status() -> dict | None:
    """加载 watchlist_status.json"""
    if not WATCHLIST_STATUS.exists():
        return None
    try:
        with open(WATCHLIST_STATUS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ═══════════════════════════════════════════
# Phase 1: 候选生成（T日 15:10）
# ═══════════════════════════════════════════

def _extract_losers(picks_history: list, lookback_days: int) -> dict:
    """从 picks_history 提取 N 天跌幅 Top100，去重（保留跌幅最深的记录）

    返回: {code: {name, change_pct, sector, drop_date, cap_label, price_label}}
    """
    losers = {}
    for i in range(min(lookback_days, len(picks_history))):
        day = picks_history[i]
        d = day.get("date", "")
        for s in day.get("top100_losers", []):
            code = s.get("code", "")
            if not code:
                continue
            pct = s.get("change_pct", 0)
            # 保留跌幅最深的记录（值最小）
            if code not in losers or pct < losers[code]["change_pct"]:
                losers[code] = {
                    "name": s.get("name", ""),
                    "change_pct": pct,
                    "sector": s.get("sector", "其他"),
                    "drop_date": d,
                    "cap_label": s.get("cap_label", ""),
                    "price_label": s.get("price_label", ""),
                }
    return losers


def _calc_stop_days(
    code: str,
    drop_date: str,
    today_df: pd.DataFrame,
    archive_data: dict,
    config: dict,
) -> tuple[int, float | None]:
    """计算止跌天数

    从大跌日之后逐日检查（使用预加载的归档数据）：
      - 涨跌幅 > stop_threshold → 止跌 +1
      - 涨跌幅 < rebounce_reset → 重置为 0（二次探底）
      - 其余 → 不计数（维持）

    返回: (stop_days, today_pct)
    """
    stop_threshold = config["stop_threshold"]
    rebounce_reset = config["rebounce_reset"]

    stop_days = 0
    drop_date_compact = drop_date.replace("-", "")

    # 按日期升序遍历归档（只看 drop_date 之后）
    for pkl_date in sorted(archive_data.keys()):
        if pkl_date <= drop_date_compact:
            continue
        df = archive_data[pkl_date]
        row = lookup_stock(df, code)
        if row is None:
            continue
        pct = float(row["涨跌幅"])
        if pct < rebounce_reset:
            stop_days = 0  # 二次探底，重置
        elif pct > stop_threshold:
            stop_days += 1

    # 今日
    today_pct = None
    today_row = lookup_stock(today_df, code)
    if today_row is not None:
        today_pct = round(float(today_row["涨跌幅"]), 2)
        if today_pct < rebounce_reset:
            stop_days = 0
        elif today_pct > stop_threshold:
            stop_days += 1

    return stop_days, today_pct


def _calc_volume_ratio(
    code: str,
    today_df: pd.DataFrame,
    archive_data: dict,
) -> float | None:
    """今日成交额 / 前5日均成交额（使用预加载的归档数据）"""
    # 今日成交额
    today_row = lookup_stock(today_df, code)
    if today_row is None:
        return None
    today_vol = float(today_row["成交额"])
    if today_vol <= 0:
        return None

    # 前5日成交额（取最近5个归档日）
    hist_vols = []
    for pkl_date in sorted(archive_data.keys(), reverse=True)[:5]:
        df = archive_data[pkl_date]
        row = lookup_stock(df, code)
        if row is not None:
            v = float(row["成交额"])
            if v > 0:
                hist_vols.append(v)

    if not hist_vols:
        return None
    avg_vol = sum(hist_vols) / len(hist_vols)
    if avg_vol <= 0:
        return None
    return round(today_vol / avg_vol, 2)


def _score_candidate(
    code: str,
    info: dict,
    stop_days: int,
    today_pct: float | None,
    volume_ratio: float | None,
    sector_context: dict,
    config: dict,
) -> dict:
    """6维打分，返回分项 + 总分"""
    w = config["weights"]
    scores = {}

    # ① 止跌天数 (max w["stop_days"])
    max_w = w["stop_days"]
    if stop_days >= 3:
        s = 15
    elif stop_days >= 2:
        s = 10
    elif stop_days >= 1:
        s = 5
    else:
        s = 0
    # 今日翻红 bonus
    if today_pct is not None and today_pct > 0:
        s = min(s + 5, max_w)
    scores["stop_days"] = s

    # ② 成交额比 (max w["volume_ratio"])
    max_w = w["volume_ratio"]
    if volume_ratio is None:
        scores["volume_ratio"] = 0
    elif volume_ratio >= 1.5:
        scores["volume_ratio"] = max_w      # 主推级
    elif volume_ratio >= 1.0:
        scores["volume_ratio"] = 10         # 观察级
    else:
        scores["volume_ratio"] = 3          # 弱量级，大幅降权

    # ③ 板块孵化 (max w["sector_incubation"])
    max_w = w["sector_incubation"]
    incub_signal = sector_context.get("incubation_signal")
    if incub_signal in ("孵化", "加速", "过热"):
        scores["sector_incubation"] = max_w
    elif sector_context.get("in_top100", 0) > 0:
        scores["sector_incubation"] = 10
    else:
        scores["sector_incubation"] = 0

    # ④ 板块密度 (max w["sector_density"])
    max_w = w["sector_density"]
    density = sector_context.get("in_top100", 0)
    if density >= 5:
        scores["sector_density"] = max_w
    elif density >= 3:
        scores["sector_density"] = 10
    elif density >= 1:
        scores["sector_density"] = 5
    else:
        scores["sector_density"] = 0

    # ⑤ 催化剂 (max w["catalyst"])
    max_w = w["catalyst"]
    if sector_context.get("in_watchlist"):
        scores["catalyst"] = max_w                          # watchlist跟踪 → 满分
    elif (sector_context.get("in_top100", 0) >= 15
          and sector_context.get("incubation_signal") == "加速"):
        scores["catalyst"] = 10                             # 板块爆发级催化
    elif (sector_context.get("in_top100", 0) >= 10
          and sector_context.get("incubation_signal") in ("孵化", "加速", "过热")):
        scores["catalyst"] = 5                              # 板块孵化级催化
    else:
        scores["catalyst"] = 0

    # ⑥ 跌幅深度 (max w["drop_depth"])
    max_w = w["drop_depth"]
    drop = info["change_pct"]  # 负数
    if drop <= -10:
        scores["drop_depth"] = max_w
    elif drop <= -8:
        scores["drop_depth"] = 10
    elif drop <= -7:
        scores["drop_depth"] = 5
    else:
        scores["drop_depth"] = 0

    scores["total"] = sum(scores.values())
    return scores


def _get_sector_context(
    sector: str,
    today_df: pd.DataFrame,
    sector_map: dict,
    incubation_alerts: list,
    watchlist_codes: set,
    code: str,
) -> dict:
    """获取板块上下文：Top100密度 + 孵化信号 + watchlist"""
    # Top100 中同板块股票数
    top100 = today_df.nlargest(100, "涨跌幅")
    in_top100 = 0
    for _, row in top100.iterrows():
        c = str(row["代码"])[2:]
        if sector_map.get(c, "其他") == sector:
            in_top100 += 1

    # 孵化信号
    incubation_signal = None
    for alert in incubation_alerts:
        if alert.get("sector") == sector:
            # 取最高优先级信号
            for sig in ["过热", "加速", "孵化"]:
                if sig in alert.get("signals", []):
                    incubation_signal = sig
                    break
            break

    # watchlist
    in_watchlist = code in watchlist_codes

    return {
        "in_top100": in_top100,
        "incubation_signal": incubation_signal,
        "in_watchlist": in_watchlist,
    }


def _build_watchlist_codes(watchlist: dict | None) -> set:
    """从 watchlist_status.json 提取所有被跟踪的 A股代码"""
    codes = set()
    if not watchlist:
        return codes
    # 趋势池
    for trend in watchlist.get("trends", []):
        for stock in trend.get("a", []):
            c = stock.get("code", "")
            if c:
                codes.add(c)
    # 板块池
    for sector in watchlist.get("sectors", []):
        for stock in sector.get("stocks", []):
            c = stock.get("code", "")
            if c:
                codes.add(c)
    return codes


def generate_candidates(config: dict = None) -> bool:
    """Phase 1: 候选生成（T日 15:10 调用）"""
    setup_logger()
    logger.info("═══ 反转追踪器 · 候选生成 ═══")

    if config is None:
        config = _load_config()

    # 1. 加载数据
    picks_history = _load_picks_history()
    if not picks_history:
        logger.error("picks_history 为空，无法生成候选")
        return False

    raw_df = load_spot()
    if raw_df is None:
        return False
    today_df = filter_valid(raw_df)
    if today_df.empty:
        logger.error("无有效个股数据")
        return False
    logger.info(f"今日有效个股: {len(today_df)}只")

    archive_data = preload_archives(max_days=10)

    sector_map = _load_sector_map()
    logger.info(f"板块映射: {len(sector_map)}只")

    # 加载 derived_intraday 获取孵化预警
    derived = _load_derived_intraday()
    incubation_alerts = []
    if derived:
        incubation_alerts = derived.get("incubation_alerts", [])
        logger.info(f"孵化预警: {len(incubation_alerts)}条")

    # 加载 watchlist
    watchlist = _load_watchlist_status()
    watchlist_codes = _build_watchlist_codes(watchlist)
    logger.info(f"watchlist 跟踪: {len(watchlist_codes)}只")

    # 2. 提取 losers
    losers = _extract_losers(picks_history, config["lookback_days"])
    logger.info(f"近{config['lookback_days']}天跌幅Top100去重: {len(losers)}只")

    # 3. 逐只计算止跌 + 打分
    candidates = []
    stopped_count = 0

    for code, info in losers.items():
        # 止跌天数
        stop_days, today_pct = _calc_stop_days(
            code, info["drop_date"], today_df, archive_data, config
        )

        # 止跌判定：至少1天止跌 或 今日翻红
        is_stopped = stop_days >= 1 or (today_pct is not None and today_pct > config["stop_threshold"])
        if not is_stopped:
            continue
        stopped_count += 1

        # 成交额比
        volume_ratio = _calc_volume_ratio(code, today_df, archive_data)

        # 板块上下文
        sector = info.get("sector", sector_map.get(code, "其他"))
        sector_ctx = _get_sector_context(
            sector, today_df, sector_map, incubation_alerts, watchlist_codes, code
        )

        # 6维打分
        scores = _score_candidate(
            code, info, stop_days, today_pct, volume_ratio, sector_ctx, config
        )

        # 最低分过滤
        if scores["total"] < config["min_score"]:
            continue

        # 涨停判定（代码前缀：300/688=20%，其他=10%）
        limit_pct = 20.0 if code.startswith(("300", "688")) else 10.0
        triggered_today = (today_pct is not None and today_pct >= limit_pct - 0.5)

        # 量比分层
        if volume_ratio is not None and volume_ratio >= 1.5:
            volume_tier = "primary"
        elif volume_ratio is not None and volume_ratio >= 1.0:
            volume_tier = "secondary"
        else:
            volume_tier = "weak"

        candidates.append({
            "code": code,
            "name": info["name"],
            "sector": sector,
            "cap_label": info.get("cap_label", ""),
            "original_drop": info["change_pct"],
            "drop_date": info["drop_date"],
            "today_pct": today_pct,
            "stop_days": stop_days,
            "volume_ratio": volume_ratio,
            "volume_tier": volume_tier,
            "triggered_today": triggered_today,
            "scores": scores,
            "sector_context": sector_ctx,
        })

    # 按总分降序
    candidates.sort(key=lambda c: c["scores"]["total"], reverse=True)
    candidates = candidates[: config["max_candidates"]]

    # ── 三因子交叉验证 ──
    # Top100 × 由弱转强 × #3板块 = 最优组合（回测结论）
    # 1) 今日Top100代码集合
    top100_codes = set()
    top100 = today_df.nlargest(100, "涨跌幅")
    for _, row in top100.iterrows():
        top100_codes.add(str(row["代码"])[2:])

    # 2) Top100板块排名（按股票数降序）
    top100_sector_rank = defaultdict(int)
    for _, row in top100.iterrows():
        c = str(row["代码"])[2:]
        sec = sector_map.get(c, "其他")
        top100_sector_rank[sec] += 1
    sector_ranking = sorted(top100_sector_rank.items(), key=lambda x: -x[1])
    rank3_sector = sector_ranking[2][0] if len(sector_ranking) >= 3 else None

    # 3) 筛选三因子共振候选
    triple_factor_picks = []
    for c in candidates:
        in_top100 = c["code"] in top100_codes
        weak_to_strong = (c["original_drop"] < 0
                          and c["today_pct"] is not None
                          and c["today_pct"] > 0)
        in_rank3 = (c["sector"] == rank3_sector) if rank3_sector else False

        # 标记因子命中情况（即使不满足三因子也写入候选，方便分析）
        c["triple_factor"] = {
            "in_top100": in_top100,
            "weak_to_strong": weak_to_strong,
            "in_rank3_sector": in_rank3,
            "rank3_sector": rank3_sector,
            "factors_hit": sum([in_top100, weak_to_strong, in_rank3]),
        }

        if in_top100 and weak_to_strong and in_rank3:
            triple_factor_picks.append({
                "code": c["code"],
                "name": c["name"],
                "sector": c["sector"],
                "today_pct": c["today_pct"],
                "volume_ratio": c["volume_ratio"],
                "volume_tier": c["volume_tier"],
                "triggered_today": c["triggered_today"],
                "score": c["scores"]["total"],
            })

    if triple_factor_picks:
        logger.info(f"🎯 三因子共振: {len(triple_factor_picks)}只 (Top100×由弱转强×{rank3_sector})")
        for p in triple_factor_picks:
            logger.info(f"   {p['name']}({p['code']}) 今{p['today_pct']}% 分{p['score']}")
    else:
        logger.info(f"三因子共振: 0只 (#3板块={rank3_sector})")

    # 4. 构建输出
    result = {
        "date": date.today().isoformat(),
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "config": {
            "stop_threshold": config["stop_threshold"],
            "rebounce_reset": config["rebounce_reset"],
            "lookback_days": config["lookback_days"],
            "lookback_mode": config["lookback_mode"],
            "min_score": config["min_score"],
        },
        "summary": {
            "scanned": len(losers),
            "stopped": stopped_count,
            "qualified": len(candidates),
            "triple_factor_count": len(triple_factor_picks),
        },
        "sector_ranking": [{"sector": s, "count": n} for s, n in sector_ranking[:5]],
        "triple_factor_picks": triple_factor_picks,
        "candidates": candidates,
    }

    # 5. 写入
    _atomic_write_json(OUTPUT_WATCHLIST, result)

    logger.info(
        f"✅ 候选生成完成: 扫描{len(losers)} → 止跌{stopped_count} → 合格{len(candidates)}"
    )
    if candidates:
        for c in candidates[:5]:
            logger.info(
                f"   {c['name']}({c['code']}) "
                f"原跌{c['original_drop']}% 今{c['today_pct']}% "
                f"止跌{c['stop_days']}天 量比{c['volume_ratio']} "
                f"总分{c['scores']['total']} [{c['sector']}]"
            )

    return True


# ═══════════════════════════════════════════
# Phase 2: 盘中监控（T+1 10:30/14:30）
# ═══════════════════════════════════════════

def monitor_candidates(slot: str = None) -> bool:
    """Phase 2: 盘中监控（读昨日 watchlist + 今日 spot）"""
    setup_logger()
    logger.info("═══ 反转追踪器 · 盘中监控 ═══")

    # 1. 加载昨日候选名单
    if not OUTPUT_WATCHLIST.exists():
        logger.info("reversal_watchlist.json 不存在，跳过监控")
        return False

    try:
        with open(OUTPUT_WATCHLIST, "r", encoding="utf-8") as f:
            watchlist = json.load(f)
    except Exception as e:
        logger.error(f"watchlist 加载失败: {e}")
        return False

    candidates = watchlist.get("candidates", [])
    if not candidates:
        logger.info("候选名单为空，跳过监控")
        return False

    watchlist_date = watchlist.get("date", "")
    logger.info(f"候选名单日期: {watchlist_date}, {len(candidates)}只")

    # 2. 加载今日 spot
    raw_df = load_spot()
    if raw_df is None:
        return False
    today_df = filter_valid(raw_df)
    if today_df.empty:
        logger.error("无有效个股数据")
        return False

    # 3. 确定时间点
    if slot is None:
        now_str = datetime.now().strftime("%H:%M")
        for s in reversed(STANDARD_SLOTS):
            if now_str >= s:
                slot = s
                break
        if slot is None:
            slot = STANDARD_SLOTS[0]
    logger.info(f"监控时间点: {slot}")

    # 4. 板块上下文（今日）
    sector_map = _load_sector_map()
    derived = _load_derived_intraday()
    incubation_alerts = derived.get("incubation_alerts", []) if derived else []

    # 今日 Top100 板块分布
    top100 = today_df.nlargest(100, "涨跌幅")
    top100_sector_counts = defaultdict(int)
    for _, row in top100.iterrows():
        c = str(row["代码"])[2:]
        sec = sector_map.get(c, "其他")
        top100_sector_counts[sec] += 1

    # 5. 逐只查今日表现
    monitor_list = []
    stats = {"total": len(candidates), "reversing": 0, "flat": 0, "falling": 0}

    for cand in candidates:
        code = cand["code"]
        code_full_sh = f"sh{code}"
        code_full_sz = f"sz{code}"

        row = today_df[
            (today_df["代码"] == code_full_sh) | (today_df["代码"] == code_full_sz)
        ]

        today_pct = None
        today_amount = None
        if not row.empty:
            today_pct = round(float(row.iloc[0]["涨跌幅"]), 2)
            today_amount = round(float(row.iloc[0]["成交额"]) / 1e8, 2)

        # 状态判定
        if today_pct is not None:
            if today_pct >= 3:
                status = "强反转"
                stats["reversing"] += 1
            elif today_pct > 0:
                status = "反转中"
                stats["reversing"] += 1
            elif today_pct > -2:
                status = "横盘"
                stats["flat"] += 1
            else:
                status = "继续跌"
                stats["falling"] += 1
        else:
            status = "无数据"
            stats["flat"] += 1

        sector = cand.get("sector", "其他")
        sector_top100_today = top100_sector_counts.get(sector, 0)

        # 板块共振：板块在Top100有票 且 有孵化信号
        sector_resonance = False
        if sector_top100_today >= 3:
            for alert in incubation_alerts:
                if alert.get("sector") == sector:
                    sector_resonance = True
                    break

        monitor_list.append({
            "code": code,
            "name": cand["name"],
            "sector": sector,
            "cap_label": cand.get("cap_label", ""),
            "original_drop": cand["original_drop"],
            "original_score": cand["scores"]["total"],
            "score_detail": cand["scores"],
            "volume_tier": cand.get("volume_tier", ""),
            "triggered_yesterday": cand.get("triggered_today", False),
            "triple_factor": cand.get("triple_factor", {}),
            "today_pct": today_pct,
            "today_amount": today_amount,
            "status": status,
            "sector_resonance": sector_resonance,
            "sector_top100_today": sector_top100_today,
        })

    # 按今日涨幅降序（强反转在前）
    monitor_list.sort(
        key=lambda m: m["today_pct"] if m["today_pct"] is not None else -999,
        reverse=True,
    )

    # 6. 输出
    result = {
        "date": date.today().isoformat(),
        "slot": slot,
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "watchlist_date": watchlist_date,
        "candidates": monitor_list,
        "stats": stats,
    }

    # 写入 slot 独立文件（避免 10:30 被 14:30 覆盖）
    slot_suffix = slot.replace(":", "")  # "10:30" → "1030"
    slot_path = DATA_DIR / f"reversal_monitor_{slot_suffix}.json"
    _atomic_write_json(slot_path, result)
    logger.info(f"写入: {slot_path.name}")

    # 同时写 latest（前端兼容，始终指向最新 slot）
    _atomic_write_json(OUTPUT_MONITOR, result)

    logger.info(
        f"✅ 盘中监控完成: {stats['total']}只 → "
        f"反转{stats['reversing']} / 横盘{stats['flat']} / 继续跌{stats['falling']}"
    )
    if monitor_list:
        for m in monitor_list[:5]:
            logger.info(
                f"   {m['name']}({m['code']}) "
                f"今{m['today_pct']}% "
                f"{'🔥共振' if m['sector_resonance'] else ''} "
                f"原分{m['original_score']} [{m['status']}]"
            )

    return True


# ═══════════════════════════════════════════
# JSON 工具
# ═══════════════════════════════════════════

def _atomic_write_json(path: Path, data: dict):
    """原子写入JSON（.tmp → rename）"""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception as e:
        logger.error(f"写入失败 {path.name}: {e}")
        if tmp.exists():
            tmp.unlink()


# ═══════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════

def run_reversal_tracker(mode: str = "generate", slot: str = None) -> bool:
    """统一入口，供 jobs.py 调用

    mode:
      "generate" → Phase 1 候选生成（T日 15:10）
      "monitor"  → Phase 2 盘中监控（T+1 10:30/14:30）
    """
    if mode == "generate":
        return generate_candidates()
    elif mode == "monitor":
        return monitor_candidates(slot=slot)
    else:
        logger.error(f"未知模式: {mode}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="反转追踪器 · python -m collector.reversal_tracker"
    )
    parser.add_argument(
        "--mode",
        choices=["generate", "monitor"],
        required=True,
        help="generate=候选生成, monitor=盘中监控",
    )
    parser.add_argument(
        "--slot",
        choices=STANDARD_SLOTS,
        help="指定时间点（monitor模式，默认自动判断）",
    )
    args = parser.parse_args()

    ok = run_reversal_tracker(mode=args.mode, slot=args.slot)
    sys.exit(0 if ok else 1)
