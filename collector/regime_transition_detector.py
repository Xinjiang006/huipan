"""
慧盘 · 市场转折检测器 v4.3
Regime Transition Detector

职责：
  每日收盘后检测14维信号（本次实现12维，C13跨市场后加），
  判断市场是否处于风格转换节点。

三组检测：
  A组(5)：旧风格瓦解 — 检测已有规律是否被打破
  B组(3)：新风格建立 — 确认底部/新主线/量能
  C组(4)：微观生态修复 — 涨停生态/大小盘/盘中结构/重合度/溢价率

输出：
  static/data/transition_scorecard.json

调度：
  15:10采集链末尾，所有采集器之后
  独立运行：python3 collector/regime_transition_detector.py
  被import：from collector.regime_transition_detector import run_transition_detector
"""

import json
import os
import logging
import statistics
import time
from datetime import datetime, date

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
CONFIG_DIR = os.path.join(BASE_DIR, "config")

# 输入文件
REGIME_PATH = os.path.join(DATA_DIR, "regime_history.json")
NEW_HIGH_LOW_PATH = os.path.join(DATA_DIR, "new_high_low.json")
INTRADAY_SNAP_PATH = os.path.join(DATA_DIR, "intraday_snapshot.json")
INTRADAY_HIST_PATH = os.path.join(DATA_DIR, "intraday_history.json")
PICKS_HIST_PATH = os.path.join(DATA_DIR, "picks_history.json")
OVERVIEW_PATH = os.path.join(DATA_DIR, "ashare_overview.json")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist_status.json")
GLOBAL_MKT_PATH = os.path.join(DATA_DIR, "global_market.json")
COMMODITIES_PATH = os.path.join(DATA_DIR, "commodities.json")
US_SECTORS_PATH = os.path.join(DATA_DIR, "us_sectors.json")

# 输出文件
SCORECARD_PATH = os.path.join(DATA_DIR, "transition_scorecard.json")

# Override文件（可选）
OVERRIDE_PATH = os.path.join(CONFIG_DIR, "transition_override.json")

# 历史保留天数
HISTORY_DAYS = 30


# ══════════════════════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════════════════════

def _load_json(path):
    """安全加载JSON文件，不存在或异常返回None"""
    name = os.path.basename(path)
    if not os.path.exists(path):
        log.debug(f"_load_json: {name} 不存在")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        dtype = type(data).__name__
        size_hint = len(data) if isinstance(data, (list, dict)) else "?"
        log.debug(f"_load_json: {name} → {dtype}({size_hint})")
        return data
    except Exception as e:
        log.warning(f"_load_json: {name} 加载失败: {e}")
        return None


def load_all_data():
    """加载所有输入数据，返回dict"""
    log.info("load_all_data: 开始加载所有输入文件")
    data = {
        "regime": _load_json(REGIME_PATH),          # list, [0]=最新
        "new_high_low": _load_json(NEW_HIGH_LOW_PATH),
        "intraday_snap": _load_json(INTRADAY_SNAP_PATH),
        "intraday_hist": _load_json(INTRADAY_HIST_PATH),
        "picks_history": _load_json(PICKS_HIST_PATH),
        "overview": _load_json(OVERVIEW_PATH),
        "watchlist": _load_json(WATCHLIST_PATH),
    }
    loaded = [k for k, v in data.items() if v is not None]
    missing = [k for k, v in data.items() if v is None]
    log.info(f"load_all_data: 已加载={loaded}, 缺失={missing}")
    if data["regime"]:
        log.debug(f"load_all_data: regime {len(data['regime'])}天, latest={data['regime'][0].get('date', '?')}")
    return data


# ══════════════════════════════════════════════════════════════
# 2. 工具函数
# ══════════════════════════════════════════════════════════════

def parse_sector_dist(raw):
    """
    解析 sector_dist_gainers/losers
    存储可能是JSON字符串(VARCHAR)或已解析的dict
    返回 dict: {板块名: 个股数} 或空dict
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def calc_hhi(sector_dist):
    """
    从板块分布dict计算HHI（赫芬达尔指数）
    sector_dist: {板块名: 个股数}
    HHI = Σ(share² × 10000)，share = 个股数/总数
    HHI > 1200 表示高度集中
    """
    if not sector_dist:
        return 0
    total = sum(sector_dist.values())
    if total == 0:
        return 0
    hhi = sum((count / total * 100) ** 2 for count in sector_dist.values())
    return round(hhi, 1)


def safe_get(d, key, default=None):
    """安全取值，None也返回default"""
    if d is None:
        return default
    val = d.get(key)
    return val if val is not None else default


def _make_result(status, **kwargs):
    """构建检测结果dict"""
    result = {"status": status}
    result.update(kwargs)
    return result


# ══════════════════════════════════════════════════════════════
# 3. A组：旧风格瓦解（5条）
# ══════════════════════════════════════════════════════════════

def check_a1_reversal_ev(regime):
    """
    A1: 抄底EV转正
    正常：rev_avg < 0
    破局：rev_avg > +0.5% 且 胜率 > 55%
    """
    log.debug("check_a1: 开始")
    if not regime or len(regime) < 1:
        return _make_result("n/a", desc="数据不足")

    latest = regime[0]
    rev_avg = safe_get(latest, "reversion_avg_return")
    rev_up = safe_get(latest, "reversion_up_count", 0)
    rev_matched = safe_get(latest, "reversion_matched", 0)

    if rev_avg is None or rev_matched == 0:
        log.debug("check_a1: T+1数据未积累")
        return _make_result("n/a", desc="T+1数据未积累")

    rev_wr = rev_up / rev_matched
    is_break = rev_avg > 0.5 and rev_wr > 0.55

    log.debug(f"check_a1: rev_avg={rev_avg:+.2f}%, wr={rev_wr*100:.0f}% → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        value=round(rev_avg, 2),
        winrate=round(rev_wr * 100, 1),
        threshold_avg=0.5,
        threshold_wr=55,
        desc=f"抄底avg={rev_avg:+.2f}%, 胜率={rev_wr*100:.0f}%"
    )


def check_a2_choppy_momentum(regime):
    """
    A2: choppy日追涨仍有效
    正常：choppy日 mom_avg ≈ 0
    破局：choppy日 mom_avg > +1.0%
    """
    log.debug("check_a2: 开始")
    if not regime or len(regime) < 1:
        return _make_result("n/a", desc="数据不足")

    latest = regime[0]
    label = safe_get(latest, "regime_label", "")
    mom_avg = safe_get(latest, "momentum_avg_return")

    if label != "choppy":
        log.debug(f"check_a2: 当前regime={label}, 非choppy, skip")
        return _make_result("n/a", desc=f"当前regime={label}，非choppy")

    if mom_avg is None:
        return _make_result("n/a", desc="T+1数据未积累")

    is_break = mom_avg > 1.0
    log.debug(f"check_a2: choppy日 mom_avg={mom_avg:+.2f}% → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        value=round(mom_avg, 2),
        threshold=1.0,
        desc=f"choppy日追涨avg={mom_avg:+.2f}%"
    )


def check_a3_momentum_streak(regime):
    """
    A3: momentum连续天数
    正常：momentum后1-2天切回
    破局：连续 ≥ 3天
    附加：判断是趋势启动还是最后一波
    """
    log.debug("check_a3: 开始")
    if not regime or len(regime) < 2:
        return _make_result("n/a", desc="数据不足")

    # 从最新往前数连续momentum天数
    streak = 0
    for day in regime[:min(len(regime), 10)]:
        if safe_get(day, "regime_label") == "momentum":
            streak += 1
        else:
            break

    is_break = streak >= 3

    # 附加判断
    sub_type = None
    if is_break and len(regime) >= streak:
        # 检查成交量趋势和HHI趋势
        volumes = []
        hhis = []
        for i in range(streak):
            vol = safe_get(regime[i], "volume_total")
            dist = parse_sector_dist(safe_get(regime[i], "sector_dist_gainers"))
            if vol is not None:
                volumes.append(vol)
            if dist:
                hhis.append(calc_hhi(dist))

        vol_increasing = (len(volumes) >= 2 and
                          all(volumes[i] <= volumes[i+1] for i in range(len(volumes)-1)))
        hhi_increasing = (len(hhis) >= 2 and
                          all(hhis[i] <= hhis[i+1] for i in range(len(hhis)-1)))

        log.debug(f"check_a3: streak={streak}, volumes={volumes[:5]}, hhis={hhis[:5]}")
        log.debug(f"check_a3: vol_increasing={vol_increasing}, hhi_increasing={hhi_increasing}")

        if vol_increasing and not hhi_increasing:
            sub_type = "趋势可能启动（放量+分散）"
        elif hhi_increasing:
            sub_type = "可能是最后一波（集中度升高）"

    log.debug(f"check_a3: streak={streak} → {'BREAK' if is_break else 'normal'}, sub_type={sub_type}")

    return _make_result(
        "break" if is_break else "normal",
        value=streak,
        threshold=3,
        sub_type=sub_type,
        desc=f"momentum连续{streak}天"
    )


def check_a4_hhi_persist(regime):
    """
    A4: HHI脉冲后不回落
    正常：HHI>1200后次日回落
    破局：HHI>1200且次日HHI仍>1000
    """
    log.debug("check_a4: 开始")
    if not regime or len(regime) < 2:
        return _make_result("n/a", desc="数据不足")

    # regime[0]=今天, regime[1]=昨天
    dist_yesterday = parse_sector_dist(safe_get(regime[1], "sector_dist_gainers"))
    dist_today = parse_sector_dist(safe_get(regime[0], "sector_dist_gainers"))

    hhi_yesterday = calc_hhi(dist_yesterday)
    hhi_today = calc_hhi(dist_today)

    log.debug(f"check_a4: hhi_yesterday={hhi_yesterday:.0f}, hhi_today={hhi_today:.0f}")

    if hhi_yesterday <= 1200:
        return _make_result("n/a",
                            hhi_yesterday=hhi_yesterday,
                            hhi_today=hhi_today,
                            desc="昨日HHI未超1200，不适用")

    is_break = hhi_today > 1000
    log.debug(f"check_a4: HHI {hhi_yesterday:.0f}→{hhi_today:.0f} → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        hhi_yesterday=hhi_yesterday,
        hhi_today=hhi_today,
        desc=f"昨日HHI={hhi_yesterday:.0f}→今日{hhi_today:.0f}"
    )


def check_a5_fade_stall(regime):
    """
    A5: 退潮板块横住不跌
    退潮定义：某板块在T-3/T-4占涨幅dist≥5只，但T-1/T占≤1只
    正常：退潮板块出现在跌幅Top5
    破局：退潮板块既不在涨幅也不在跌幅Top5（横住了）
    """
    log.debug("check_a5: 开始")
    if not regime or len(regime) < 5:
        return _make_result("n/a", desc="数据不足（需要5天）")

    # 取最近5天的板块分布
    days_gainers = []
    days_losers = []
    for i in range(min(5, len(regime))):
        g = parse_sector_dist(safe_get(regime[i], "sector_dist_gainers"))
        l = parse_sector_dist(safe_get(regime[i], "sector_dist_losers"))
        days_gainers.append(g)
        days_losers.append(l)

    # 找退潮板块：T-3/T-4(index 3,4)占≥5只，T/T-1(index 0,1)占≤1只
    faded_sectors = []
    all_sectors = set()
    for dg in days_gainers:
        all_sectors.update(dg.keys())

    for sector in all_sectors:
        # 过去（index 3,4 = T-3, T-4）
        old_avg = 0
        old_count = 0
        for i in [3, 4]:
            if i < len(days_gainers):
                old_avg += days_gainers[i].get(sector, 0)
                old_count += 1
        old_avg = old_avg / max(old_count, 1)

        # 近期（index 0,1 = T, T-1）
        new_avg = 0
        new_count = 0
        for i in [0, 1]:
            if i < len(days_gainers):
                new_avg += days_gainers[i].get(sector, 0)
                new_count += 1
        new_avg = new_avg / max(new_count, 1)

        if old_avg >= 5 and new_avg <= 1:
            faded_sectors.append(sector)

    log.debug(f"check_a5: 退潮板块={faded_sectors}")

    if not faded_sectors:
        return _make_result("normal", desc="未检测到退潮板块")

    # 检查退潮板块是否出现在跌幅Top5
    stalled = []
    for sector in faded_sectors:
        in_loser_top5 = False
        for i in [0, 1]:
            if i < len(days_losers):
                # 跌幅Top5 = 跌幅dist中个股数最多的5个板块
                sorted_losers = sorted(days_losers[i].items(),
                                       key=lambda x: x[1], reverse=True)
                top5_sectors = [s[0] for s in sorted_losers[:5]]
                if sector in top5_sectors:
                    in_loser_top5 = True
                    break

        in_gainer = any(
            days_gainers[i].get(sector, 0) >= 3
            for i in [0, 1] if i < len(days_gainers)
        )

        if not in_loser_top5 and not in_gainer:
            stalled.append(sector)

    is_break = len(stalled) > 0
    log.debug(f"check_a5: faded={faded_sectors}, stalled={stalled} → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        faded_sectors=faded_sectors,
        stalled_sectors=stalled,
        desc=f"退潮板块{faded_sectors}，横住{stalled}" if stalled
             else f"退潮板块{faded_sectors}已进跌幅榜"
    )


# ══════════════════════════════════════════════════════════════
# 4. B组：新风格建立（3条）
# ══════════════════════════════════════════════════════════════

def check_b6_new_mainline(regime, watchlist):
    """
    B6: 新主线出现
    4个子条件（满足≥3触发）：
      ① 板块厚度：某板块连续2天在gainers占≥5只
      ② 新面孔：该板块过去5天日均占≤2只
      ③ 机构参与：micro_cap_ratio_gainer < 70%
      ④ 催化剂升温：news_heat trend=up
    """
    log.debug("check_b6: 开始")
    if not regime or len(regime) < 3:
        return _make_result("n/a", desc="数据不足", candidates=[], sub_scores="0/4")

    # 取最近5天gainers分布
    days = min(5, len(regime))
    daily_dists = []
    for i in range(days):
        dist = parse_sector_dist(safe_get(regime[i], "sector_dist_gainers"))
        daily_dists.append(dist)

    # 找所有板块
    all_sectors = set()
    for dd in daily_dists:
        all_sectors.update(dd.keys())

    log.debug(f"check_b6: {days}天数据, {len(all_sectors)} 个板块")

    candidates = []
    for sector in all_sectors:
        sub = [False, False, False, False]

        # ① 板块厚度：最近2天（index 0,1）连续占≥5只
        recent_counts = [daily_dists[i].get(sector, 0) for i in range(min(2, days))]
        if len(recent_counts) >= 2 and all(c >= 5 for c in recent_counts):
            sub[0] = True

        # ② 新面孔：过去5天日均占≤2
        all_counts = [daily_dists[i].get(sector, 0) for i in range(days)]
        avg_count = sum(all_counts) / len(all_counts) if all_counts else 0
        if avg_count <= 2 and sub[0]:  # 只在厚度满足时才检查新面孔
            sub[1] = True

        # ③ 机构参与（全局指标，不按板块）
        micro_ratio = safe_get(regime[0], "micro_cap_ratio_gainer")
        if micro_ratio is not None and micro_ratio < 70:
            sub[2] = True

        # ④ 催化剂（从watchlist中找匹配趋势）
        if watchlist and isinstance(watchlist, dict):
            trends = watchlist.get("trends", [])
            for trend in trends:
                heat = trend.get("news_heat", {})
                if heat.get("trend") == "up":
                    # 粗略匹配：趋势名称是否包含板块关键词
                    trend_name = trend.get("name", "")
                    if sector in trend_name or any(
                        kw in sector for kw in trend.get("keywords", [])
                    ):
                        sub[3] = True

        score = sum(sub)
        if score >= 2:
            log.debug(f"check_b6: {sector} sub={sub}, score={score}/4, thickness={recent_counts}, avg5d={avg_count:.1f}")
        if score >= 3:
            candidates.append({
                "sector": sector,
                "sub_scores": f"{score}/4",
                "thickness": recent_counts,
                "avg_5d": round(avg_count, 1),
                "subs": sub
            })

    is_confirmed = len(candidates) > 0
    log.debug(f"check_b6: → {'CONFIRMED' if is_confirmed else 'normal'}, candidates={[c['sector'] for c in candidates]}")

    return _make_result(
        "confirmed" if is_confirmed else "normal",
        candidates=candidates,
        sub_scores=f"{len(candidates)} mainline(s)",
        desc=f"检测到新主线: {[c['sector'] for c in candidates]}" if candidates
             else "未检测到新主线"
    )


def check_b7_bottom(regime, new_high_low):
    """
    B7: 探底确认
    5个子条件（满足≥3触发）：
      ① 跌无新肉：ls_prev5_same > 65%
      ② 轮杀停滞：过去5天跌幅Top5板块无新增行业
      ③ 跌幅收窄：近3天rev_avg绝对值递减
      ④ 新低转降：low_year.total峰值后连降≥2天
      ⑤ 波动压缩：volatility_5d较近5天峰值降≥30%
    """
    log.debug("check_b7: 开始")
    if not regime or len(regime) < 3:
        return _make_result("n/a", desc="数据不足", sub_scores="0/5",
                            subs={})

    subs = {}

    # ① 跌无新肉
    ls_prev5 = safe_get(regime[0], "ls_prev5_same")
    if ls_prev5 is not None:
        subs["no_new_blood"] = ls_prev5 > 65
        log.debug(f"check_b7: ① ls_prev5_same={ls_prev5:.1f} → {'✓' if subs['no_new_blood'] else '✗'}")
    else:
        subs["no_new_blood"] = None
        log.debug("check_b7: ① ls_prev5_same=None")

    # ② 轮杀停滞
    if len(regime) >= 5:
        all_loser_sectors = set()
        new_each_day = []
        for i in range(min(5, len(regime)) - 1, -1, -1):  # 从旧到新
            dist = parse_sector_dist(safe_get(regime[i], "sector_dist_losers"))
            sorted_l = sorted(dist.items(), key=lambda x: x[1], reverse=True)
            top5 = set(s[0] for s in sorted_l[:5])
            new_sectors = top5 - all_loser_sectors
            new_each_day.append(len(new_sectors))
            all_loser_sectors.update(top5)
        # 最近2天没有新行业进入跌幅Top5
        subs["washout_stalled"] = (len(new_each_day) >= 2 and
                                    new_each_day[-1] == 0 and
                                    new_each_day[-2] == 0)
        log.debug(f"check_b7: ② new_each_day={new_each_day} → {'✓' if subs['washout_stalled'] else '✗'}")
    else:
        subs["washout_stalled"] = None

    # ③ 跌幅收窄
    if len(regime) >= 3:
        rev_avgs = []
        for i in range(3):
            ra = safe_get(regime[i], "reversion_avg_return")
            if ra is not None:
                rev_avgs.append(abs(ra))
            else:
                rev_avgs.append(None)
        if all(v is not None for v in rev_avgs):
            # rev_avgs[0]=今天, [1]=昨天, [2]=前天
            # 递减 = 今天 < 昨天 < 前天
            subs["decline_narrowing"] = (rev_avgs[0] < rev_avgs[1] < rev_avgs[2])
            log.debug(f"check_b7: ③ rev_avgs_abs={[round(v,2) for v in rev_avgs]} → {'✓' if subs['decline_narrowing'] else '✗'}")
        else:
            subs["decline_narrowing"] = None
            log.debug(f"check_b7: ③ rev_avgs有None: {rev_avgs}")
    else:
        subs["decline_narrowing"] = None

    # ④ 新低转降
    if new_high_low and "history" in new_high_low:
        hist = new_high_low["history"]
        low_series = []
        for h in hist[:10]:  # 最近10天
            ly = h.get("low_year", {})
            total = ly.get("total", 0) if isinstance(ly, dict) else 0
            low_series.append(total)
        # 找峰值后连降
        if len(low_series) >= 3:
            # low_series[0]=最新
            peak_idx = low_series.index(max(low_series))
            if peak_idx >= 2:
                # 峰值在第3天或更早，检查峰值后是否连降
                descending = all(
                    low_series[i] >= low_series[i-1]
                    for i in range(peak_idx, 0, -1)
                )
                subs["new_low_declining"] = descending and peak_idx >= 2
            else:
                subs["new_low_declining"] = False
            log.debug(f"check_b7: ④ low_series={low_series[:5]}, peak_idx={peak_idx} → {subs['new_low_declining']}")
        else:
            subs["new_low_declining"] = None
    else:
        subs["new_low_declining"] = None
        log.debug("check_b7: ④ new_high_low 数据缺失")

    # ⑤ 波动压缩
    if len(regime) >= 5:
        vols = []
        for i in range(min(5, len(regime))):
            v = safe_get(regime[i], "volatility_5d")
            if v is not None:
                vols.append(v)
        if len(vols) >= 3:
            peak_vol = max(vols)
            current_vol = vols[0]
            if peak_vol > 0:
                drop_pct = (peak_vol - current_vol) / peak_vol
                subs["vol_compressed"] = drop_pct >= 0.30
                log.debug(f"check_b7: ⑤ vol peak={peak_vol:.2f} current={current_vol:.2f} drop={drop_pct:.0%} → {subs['vol_compressed']}")
            else:
                subs["vol_compressed"] = None
        else:
            subs["vol_compressed"] = None
    else:
        subs["vol_compressed"] = None

    # 计分（None不计入分母）
    valid_subs = {k: v for k, v in subs.items() if v is not None}
    score = sum(1 for v in valid_subs.values() if v)
    total = len(valid_subs)
    is_confirmed = score >= 3

    log.debug(f"check_b7: subs={subs} → score={score}/{total} → {'CONFIRMED' if is_confirmed else 'normal'}")

    return _make_result(
        "confirmed" if is_confirmed else "normal",
        sub_scores=f"{score}/{total}",
        subs=subs,
        desc=f"探底子条件 {score}/{total}"
    )


def check_b8_volume(regime):
    """
    B8: 量能确认
    三阶段：缩量期 → 地量确认 → 放量反转
    """
    log.debug("check_b8: 开始")
    if not regime or len(regime) < 5:
        return _make_result("n/a", desc="数据不足", phase="unknown")

    # 取最近数据
    vol_ranks = []
    up_ratios = []
    volumes = []
    for i in range(min(10, len(regime))):
        vr = safe_get(regime[i], "volume_rank_30d")
        ur = safe_get(regime[i], "up_ratio")
        vt = safe_get(regime[i], "volume_total")
        if vr is not None:
            vol_ranks.append(vr)
        if ur is not None:
            up_ratios.append(ur)
        if vt is not None:
            volumes.append(vt)

    if not vol_ranks:
        return _make_result("n/a", desc="volume_rank数据缺失", phase="unknown")

    current_rank = vol_ranks[0]
    phase = "unknown"

    log.debug(f"check_b8: vol_ranks={vol_ranks[:5]}, current_rank={current_rank}")

    # 阶段3：放量反转（最先检查，优先级最高）
    if len(vol_ranks) >= 2:
        rank_jump = vol_ranks[1] - vol_ranks[0]  # 排名跳升（数字变小=排名升高）
        vol_5d_avg = (sum(volumes[1:6]) / len(volumes[1:6])) if len(volumes) > 1 else 0
        vol_ratio = volumes[0] / vol_5d_avg if vol_5d_avg > 0 else 0
        up_r = up_ratios[0] if up_ratios else 0

        log.debug(f"check_b8: rank_jump={rank_jump}, vol_ratio={vol_ratio:.2f}, up_ratio={up_r:.0f}")

        if rank_jump >= 10 and up_r > 55 and vol_ratio > 1.5:
            phase = "放量反转"
            log.debug(f"check_b8: → CONFIRMED 放量反转")
            return _make_result(
                "confirmed", phase=phase,
                value=current_rank, rank_jump=rank_jump,
                vol_ratio=round(vol_ratio, 2), up_ratio=up_r,
                desc=f"排名跳升{rank_jump}位, 量比{vol_ratio:.1f}x, 上涨{up_r:.0f}%"
            )

    # 阶段2：地量确认
    if current_rank >= 27:
        phase = "地量确认"
        log.debug(f"check_b8: → normal 地量确认 (rank={current_rank})")
        return _make_result(
            "normal", phase=phase,
            value=current_rank,
            desc=f"成交排名{current_rank}/30，地量"
        )

    # 阶段1：缩量期
    shrink_days = sum(1 for vr in vol_ranks[:5] if vr > 20)
    if shrink_days >= 3:
        phase = "缩量期"
    else:
        phase = "正常"

    log.debug(f"check_b8: → normal {phase} (rank={current_rank}, shrink={shrink_days})")

    return _make_result(
        "normal", phase=phase,
        value=current_rank,
        shrink_days=shrink_days,
        desc=f"成交排名{current_rank}/30，近5天{shrink_days}天缩量"
    )


# ══════════════════════════════════════════════════════════════
# 5. C组：微观生态修复（5条，C13占位）
# ══════════════════════════════════════════════════════════════

def check_c9_zt_ecosystem(overview):
    """
    C9: 涨停生态修复
    子条件（满足≥2触发）：
      ① 炸板率 < 25%
      ② 连板存活率 > 50%
      ③ 涨停板块集中（暂不实现，降级跳过）
    """
    log.debug("check_c9: 开始")
    if not overview or "kpi" not in overview:
        log.debug("check_c9: overview数据缺失")
        return _make_result("n/a", desc="overview数据缺失")

    kpi = overview["kpi"]
    zha_ban = safe_get(kpi, "zha_ban_rate")
    lb_survived = safe_get(kpi, "lianban_survived", 0)
    lb_total = safe_get(kpi, "lianban_total", 0)

    subs = {}

    # ① 炸板率
    if zha_ban is not None:
        # zha_ban_rate 可能是字符串"33.3"或数字
        zha_val = float(zha_ban) if isinstance(zha_ban, str) else zha_ban
        subs["zha_ban_low"] = zha_val < 25
        log.debug(f"check_c9: ① zha_ban={zha_val:.1f}% → {'✓' if subs['zha_ban_low'] else '✗'}")
    else:
        subs["zha_ban_low"] = None
        zha_val = None

    # ② 连板存活率
    if lb_total > 0:
        lb_ratio = lb_survived / lb_total
        subs["lianban_healthy"] = lb_ratio > 0.5
        log.debug(f"check_c9: ② lianban {lb_survived}/{lb_total}={lb_ratio:.0%} → {'✓' if subs['lianban_healthy'] else '✗'}")
    else:
        subs["lianban_healthy"] = None

    # ③ 涨停板块集中（暂不实现）
    subs["zt_sector_concentrated"] = None

    valid = {k: v for k, v in subs.items() if v is not None}
    score = sum(1 for v in valid.values() if v)
    is_break = score >= 2

    log.debug(f"check_c9: subs={subs} → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        zha_ban=zha_val if zha_ban is not None else None,
        lianban_ratio=round(lb_survived / lb_total, 2) if lb_total > 0 else None,
        subs=subs,
        desc=f"炸板率{zha_val:.0f}%, 连板{lb_survived}/{lb_total}"
             if zha_ban is not None else "数据不完整"
    )


def check_c10_cap_scissors(regime):
    """
    C10: 大小盘剪刀差反转
    破局：微盘追涨avg连续2天好于大盘追涨avg
    """
    log.debug("check_c10: 开始")
    if not regime or len(regime) < 2:
        return _make_result("n/a", desc="数据不足")

    diffs = []  # micro - large, 正=微盘更好
    for i in range(min(3, len(regime))):
        micro = safe_get(regime[i], "mom_micro_avg")
        large = safe_get(regime[i], "mom_large_avg")
        if micro is not None and large is not None:
            diffs.append(round(micro - large, 2))
        else:
            diffs.append(None)

    # 连续2天微盘好于大盘
    is_break = (len(diffs) >= 2 and
                diffs[0] is not None and diffs[1] is not None and
                diffs[0] > 0 and diffs[1] > 0)

    log.debug(f"check_c10: micro-large diffs={diffs[:3]} → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        micro_vs_large=diffs[:3],
        desc=f"微盘-大盘差: {diffs[:3]}"
    )


def check_c11_intraday(intraday_snap):
    """
    C11: 盘中结构变化
    破局：open_bear组日内反弹为正（开盘低开的股票收盘时反弹了）
    """
    log.debug("check_c11: 开始")
    if not intraday_snap:
        return _make_result("n/a", desc="盘中数据缺失")

    snapshots = intraday_snap.get("snapshots", [])
    if not snapshots:
        return _make_result("n/a", desc="无盘中快照")

    log.debug(f"check_c11: {len(snapshots)} 个快照")

    # 取最后一个snapshot（通常是15:10的）
    last_snap = snapshots[-1]
    open_bear = last_snap.get("open_bear", {})
    # 字段名在实际数据中是 "avg"，不是 "avg_change"
    ob_avg = safe_get(open_bear, "avg")
    if ob_avg is None:
        ob_avg = safe_get(open_bear, "avg_change")  # fallback兼容

    if ob_avg is None:
        log.debug("check_c11: open_bear数据缺失")
        return _make_result("n/a", desc="open_bear数据缺失")

    is_break = ob_avg > 0

    log.debug(f"check_c11: open_bear_avg={ob_avg:+.2f}% → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        open_bear_avg=round(ob_avg, 2),
        desc=f"开盘低开组日内变化: {ob_avg:+.2f}%"
    )


def check_c12_overlap(picks_history):
    """
    C12: 涨跌Top100重合度变化
    破局：涨幅重合率>40% 或 跌幅重合率>50%
    """
    log.debug("check_c12: 开始")
    if not picks_history:
        return _make_result("n/a", desc="picks_history不存在",
                            gainer_overlap=None, loser_overlap=None)

    # picks_history可能是list或dict格式
    # dict格式: {date_str: {top100_gainers: [...], top100_losers: [...]}}
    # list格式: [{date, top100_gainers, top100_losers}, ...]
    days_list = []
    if isinstance(picks_history, dict):
        for dt in sorted(picks_history.keys(), reverse=True):
            val = picks_history[dt]
            if isinstance(val, dict):
                days_list.append(val)
        log.debug(f"check_c12: dict格式, {len(days_list)} 天")
    elif isinstance(picks_history, list):
        days_list = picks_history
        log.debug(f"check_c12: list格式, {len(days_list)} 天")
    
    if len(days_list) < 2:
        return _make_result("n/a", desc=f"picks_history仅{len(days_list)}天，需≥2天",
                            gainer_overlap=None, loser_overlap=None)

    def calc_overlap(day1, day2, group_key):
        """计算两天同组Top100的重合率"""
        codes1 = set()
        codes2 = set()
        for item in day1.get(group_key, []):
            code = item.get("code", item) if isinstance(item, dict) else item
            codes1.add(str(code))
        for item in day2.get(group_key, []):
            code = item.get("code", item) if isinstance(item, dict) else item
            codes2.add(str(code))
        if not codes1 or not codes2:
            return None
        overlap = len(codes1 & codes2)
        return round(overlap / max(len(codes1), 1) * 100, 1)

    # picks_history: list of daily picks, [0]=最新
    gainer_overlap = calc_overlap(days_list[0], days_list[1],
                                   "top100_gainers")
    loser_overlap = calc_overlap(days_list[0], days_list[1],
                                  "top100_losers")

    is_break = False
    if gainer_overlap is not None and gainer_overlap > 40:
        is_break = True
    if loser_overlap is not None and loser_overlap > 50:
        is_break = True

    log.debug(f"check_c12: gainer_overlap={gainer_overlap}%, loser_overlap={loser_overlap}% → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        gainer_overlap=gainer_overlap,
        loser_overlap=loser_overlap,
        desc=f"涨幅重合{gainer_overlap}%, 跌幅重合{loser_overlap}%"
    )


def check_c14_zt_premium(regime):
    """
    C14: 涨停溢价率底部信号
    破局：连续2天 zt_premium > +1.0%
    """
    log.debug("check_c14: 开始")
    if not regime or len(regime) < 2:
        return _make_result("n/a", desc="数据不足")

    premiums = []
    for i in range(min(3, len(regime))):
        # 实际字段名是 zt_premium_avg，fallback兼容 zt_premium
        p = safe_get(regime[i], "zt_premium_avg")
        if p is None:
            p = safe_get(regime[i], "zt_premium")
        premiums.append(p)

    # 连续2天 > 1.0
    valid = [p for p in premiums[:2] if p is not None]
    if len(valid) < 2:
        return _make_result("n/a", values=premiums[:3], desc="zt_premium数据不足")

    is_break = all(p > 1.0 for p in valid)

    log.debug(f"check_c14: premiums={premiums[:3]} → {'BREAK' if is_break else 'normal'}")

    return _make_result(
        "break" if is_break else "normal",
        values=[round(p, 2) if p is not None else None for p in premiums[:3]],
        desc=f"涨停溢价率: {premiums[:3]}"
    )


# ══════════════════════════════════════════════════════════════
# 6. 周期阶段判定
# ══════════════════════════════════════════════════════════════

def determine_phase(a_score, b_score, c_score, details, regime):
    """
    从检测结果推断当前市场周期阶段
    返回阶段标签（英文）
    
    v4.3.1 调整：
    - 加入前置分析区分"技术反弹"vs"真转折"
    - mean_reversion在反弹日归为weak（补跌阶段的一日游）
    - 新增 technical_bounce 判定
    """
    log.debug(f"determine_phase: A={a_score}, B={b_score}, C={c_score}")
    if not regime:
        return "uncertain"

    # ─── 判断今天是否是"技术反弹日" ───
    # 条件：regime=mean_reversion + 前1日涨幅组同向率低 + 跌幅组前期涨幅大
    is_bounce = False
    latest = regime[0]
    label_today = safe_get(latest, "regime_label", "")
    gn_prev1_same = safe_get(latest, "gn_prev1_same")
    ls_prev5_avg = safe_get(latest, "ls_prev5_avg")
    
    if label_today == "mean_reversion":
        # 涨幅组前1日同向率<35%：今天涨的大部分昨天在跌 → 超跌反弹
        # 跌幅组前5日均值>5%：今天跌的前5天平均涨了很多 → 多杀多
        bounce_signals = 0
        if gn_prev1_same is not None and gn_prev1_same < 35:
            bounce_signals += 1
        if ls_prev5_avg is not None and ls_prev5_avg > 5:
            bounce_signals += 1
        # C组信号少（<2）也是反弹的佐证 — 微观生态没修复
        if c_score < 2:
            bounce_signals += 1
        if bounce_signals >= 2:
            is_bounce = True
        log.debug(f"determine_phase: mean_reversion检测 → bounce_signals={bounce_signals}, is_bounce={is_bounce}")

    # ─── 阶段判定（按优先级） ───

    # 新周期：多维共振（不受反弹影响，因为需要B+C组共同确认）
    if a_score >= 2 and b_score >= 2 and c_score >= 3:
        log.info(f"determine_phase: → new_cycle (A≥2 B≥2 C≥3)")
        return "new_cycle"

    # 探底：底部信号出现
    if b_score >= 2 and c_score >= 2:
        log.info(f"determine_phase: → bottoming (B≥2 C≥2)")
        return "bottoming"

    # 杀不动：轮杀停滞 + 跌幅收窄
    b7 = details.get("b7_bottom", {})
    b7_subs = b7.get("subs", {})
    if (b7_subs.get("no_new_blood") is True and
        b7_subs.get("decline_narrowing") is True):
        log.info(f"determine_phase: → exhaustion (no_new_blood+decline_narrowing)")
        return "exhaustion"

    # 构建最近5天label序列
    labels = [safe_get(regime[i], "regime_label")
              for i in range(min(5, len(regime)))]
    log.debug(f"determine_phase: labels={labels}")

    # 技术反弹日：仍算补跌/多杀多阶段的一部分
    if is_bounce:
        # 检查剩余几天是否偏弱
        other_labels = labels[1:]  # 除今天外
        weak_other = sum(1 for l in other_labels
                         if l in ("choppy", "trending_down", "mean_reversion"))
        if weak_other >= 2:
            log.info(f"determine_phase: → sector_washout (bounce + weak_other={weak_other})")
            return "sector_washout"
        else:
            log.info(f"determine_phase: → long_squeeze (bounce + weak_other={weak_other})")
            return "long_squeeze"

    # 多杀多：强转弱信号
    ls_prev3 = safe_get(regime[0], "ls_prev3_same")
    if ls_prev3 is not None and ls_prev3 < 45:
        # 检查近期是否偏弱
        non_momentum = sum(1 for l in labels[:3]
                           if l in ("choppy", "trending_down", "mean_reversion"))
        if non_momentum >= 2:
            log.info(f"determine_phase: → long_squeeze (ls_prev3={ls_prev3:.0f}<45, non_mom={non_momentum})")
            return "long_squeeze"

    # 补跌轮杀：近5天多数偏弱
    # mean_reversion在非反弹日也算weak（抄底有效=市场在低位）
    weak_count = sum(1 for l in labels
                     if l in ("choppy", "trending_down", "mean_reversion"))
    if weak_count >= 3:
        log.info(f"determine_phase: → sector_washout (weak_count={weak_count})")
        return "sector_washout"

    # 上涨轮动：连续momentum
    momentum_count = sum(1 for l in labels[:3] if l == "momentum")
    if momentum_count >= 2:
        log.info(f"determine_phase: → uptrend_rotation (momentum_count={momentum_count})")
        return "uptrend_rotation"

    log.info(f"determine_phase: → uncertain")
    return "uncertain"


PHASE_CN = {
    "uptrend_rotation": "上涨轮动",
    "long_squeeze": "多杀多",
    "sector_washout": "补跌轮杀",
    "exhaustion": "杀不动",
    "bottoming": "探底",
    "new_cycle": "新周期",
    "uncertain": "待判定",
}


# ══════════════════════════════════════════════════════════════
# 7. 信号分级
# ══════════════════════════════════════════════════════════════

def determine_signal_level(a_score, b_score, c_score):
    """
    信号分级：none / early / medium / strong
    """
    if a_score >= 2 and b_score >= 2 and c_score >= 3:
        level = "strong"
    elif a_score >= 2 and b_score >= 1 and c_score >= 2:
        level = "medium"
    elif a_score >= 2 or c_score >= 2:
        level = "early"
    else:
        level = "none"
    log.debug(f"determine_signal_level: A={a_score} B={b_score} C={c_score} → {level}")
    return level


LEVEL_DESC = {
    "none": "当前风格延续，无转折信号",
    "early": "出现早期信号，关注但不行动",
    "medium": "中等信号，建议LLM深度分析",
    "strong": "强转折信号，准备切换策略",
}


# ══════════════════════════════════════════════════════════════
# 8. 历史管理
# ══════════════════════════════════════════════════════════════

def load_history():
    """加载历史scorecard记录"""
    data = _load_json(SCORECARD_PATH)
    if data and "history" in data:
        log.debug(f"load_history: {len(data['history'])} 条历史记录")
        return data["history"]
    log.debug("load_history: 无历史记录")
    return []


def load_override():
    """加载手动override配置"""
    data = _load_json(OVERRIDE_PATH)
    if not data:
        log.debug("load_override: 无override配置")
        return None
    override_date = data.get("override_date", "")
    today_str = date.today().strftime("%Y-%m-%d")
    if override_date == today_str:
        log.info(f"load_override: 有效override → phase={data.get('override_phase')}, reason={data.get('override_reason','')}")
        return data
    log.debug(f"load_override: override已过期 ({override_date} != {today_str})")
    return None  # 过期


# ══════════════════════════════════════════════════════════════
# 9. 阶段天数计算
# ══════════════════════════════════════════════════════════════

def calc_phase_day(current_phase, history):
    """计算当前阶段持续天数"""
    days = 1
    for h in history:
        if h.get("phase") == current_phase:
            days += 1
        else:
            break
    log.debug(f"calc_phase_day: {current_phase} → 第{days}天")
    return days


# ══════════════════════════════════════════════════════════════
# 10. 主入口
# ══════════════════════════════════════════════════════════════

def run_transition_detector():
    """
    主入口函数，执行完整检测流程
    """
    t_start = time.time()
    log.info("═══ 市场转折检测 开始 ═══")

    # 加载数据
    data = load_all_data()
    regime = data["regime"]

    if not regime or len(regime) < 1:
        log.warning("regime_history为空，跳过检测")
        return None

    today_str = date.today().strftime("%Y-%m-%d")
    log.info(f"run_transition_detector: 检测日期={today_str}, regime天数={len(regime)}")

    # ─── A组 ───
    log.info("── A组: 旧风格瓦解 ──")
    a_results = {
        "a1_reversal_ev": check_a1_reversal_ev(regime),
        "a2_choppy_mom": check_a2_choppy_momentum(regime),
        "a3_mom_streak": check_a3_momentum_streak(regime),
        "a4_hhi_persist": check_a4_hhi_persist(regime),
        "a5_fade_stall": check_a5_fade_stall(regime),
    }
    a_score = sum(1 for r in a_results.values()
                  if r["status"] == "break")
    a_breaks = [k for k, v in a_results.items() if v["status"] == "break"]
    log.info(f"A组: {a_score}/5, breaks={a_breaks}")

    # ─── B组 ───
    log.info("── B组: 新风格建立 ──")
    b_results = {
        "b6_new_mainline": check_b6_new_mainline(regime, data["watchlist"]),
        "b7_bottom": check_b7_bottom(regime, data["new_high_low"]),
        "b8_volume": check_b8_volume(regime),
    }
    b_score = sum(1 for r in b_results.values()
                  if r["status"] == "confirmed")
    b_confirmed = [k for k, v in b_results.items() if v["status"] == "confirmed"]
    log.info(f"B组: {b_score}/3, confirmed={b_confirmed}")

    # ─── C组 ───
    log.info("── C组: 微观生态修复 ──")
    c_results = {
        "c9_zt_ecosystem": check_c9_zt_ecosystem(data["overview"]),
        "c10_cap_scissors": check_c10_cap_scissors(regime),
        "c11_intraday": check_c11_intraday(data["intraday_snap"]),
        "c12_overlap": check_c12_overlap(data["picks_history"]),
        "c14_zt_premium": check_c14_zt_premium(regime),
    }
    c_score = sum(1 for r in c_results.values()
                  if r["status"] == "break")
    c_breaks = [k for k, v in c_results.items() if v["status"] == "break"]
    log.info(f"C组: {c_score}/5, breaks={c_breaks}")

    # ─── 汇总 ───
    all_details = {}
    all_details.update(a_results)
    all_details.update(b_results)
    all_details.update(c_results)

    # 阶段判定
    phase_auto = determine_phase(a_score, b_score, c_score,
                                  all_details, regime)

    # 反弹日检测（用于scorecard输出）
    is_bounce = False
    bounce_clues = []
    latest = regime[0]
    if safe_get(latest, "regime_label") == "mean_reversion":
        gn_p1 = safe_get(latest, "gn_prev1_same")
        ls_p5 = safe_get(latest, "ls_prev5_avg")
        if gn_p1 is not None and gn_p1 < 35:
            bounce_clues.append(f"涨幅组前1日同向仅{gn_p1:.0f}%")
        if ls_p5 is not None and ls_p5 > 5:
            bounce_clues.append(f"跌幅组前5日均值+{ls_p5:.1f}%")
        if c_score < 2:
            bounce_clues.append(f"C组仅{c_score}/5")
        if len(bounce_clues) >= 2:
            is_bounce = True
            log.info(f"技术反弹日检测: is_bounce=True, clues={bounce_clues}")

    # Override检查
    override = load_override()
    phase_override = None
    if override:
        phase_override = override.get("override_phase")
        log.info(f"📌 手动override: {phase_override} ({override.get('override_reason','')})")

    current_phase = phase_override if phase_override else phase_auto

    # 信号分级
    signal_level = determine_signal_level(a_score, b_score, c_score)

    # 历史
    history = load_history()
    phase_day = calc_phase_day(current_phase, history)

    # 构建scorecard
    scorecard = {
        "date": today_str,
        "current_phase": current_phase,
        "current_phase_cn": PHASE_CN.get(current_phase, "未知"),
        "phase_auto": phase_auto,
        "phase_override": phase_override,
        "phase_day": phase_day,

        "group_a": {
            "score": a_score,
            "total": 5,
            "details": a_results,
        },
        "group_b": {
            "score": b_score,
            "total": 3,
            "details": b_results,
        },
        "group_c": {
            "score": c_score,
            "total": 5,  # C13未实现，total=5不是6
            "details": c_results,
        },

        "summary": {
            "signal_level": signal_level,
            "signal_level_desc": LEVEL_DESC.get(signal_level, ""),
            "a_score": f"{a_score}/5",
            "b_score": f"{b_score}/3",
            "c_score": f"{c_score}/5",
            "is_bounce": is_bounce,
            "bounce_clues": bounce_clues if is_bounce else [],
            "interpretation": _build_interpretation(
                a_score, b_score, c_score, signal_level, current_phase,
                all_details, is_bounce
            ),
            "llm_trigger": signal_level in ("medium", "strong"),
        },

        "history": _update_history(history, today_str, current_phase,
                                     a_score, b_score, c_score, signal_level),
    }

    # 保存
    try:
        os.makedirs(os.path.dirname(SCORECARD_PATH), exist_ok=True)
        with open(SCORECARD_PATH, "w", encoding="utf-8") as f:
            json.dump(scorecard, f, ensure_ascii=False, indent=2)
        log.info(f"scorecard已保存 → {SCORECARD_PATH}")
    except Exception as e:
        log.error(f"scorecard保存失败: {e}")

    # 打印摘要
    _print_summary(scorecard)

    elapsed = time.time() - t_start
    log.info(f"═══ 市场转折检测 完成 ({elapsed:.1f}s) ═══")

    return scorecard


def _build_interpretation(a, b, c, level, phase, details, is_bounce=False):
    """构建自然语言解读"""
    parts = []
    phase_cn = PHASE_CN.get(phase, phase)
    parts.append(f"当前阶段: {phase_cn}")

    if is_bounce:
        parts.append("⚠️ 今日为技术反弹日(非风格转换)")

    if level == "none":
        if not is_bounce:
            parts.append("14维检测均正常，当前风格延续")
    elif level == "early":
        # 找出哪些维度break了
        breaks = [k for k, v in details.items() if v.get("status") == "break"]
        parts.append(f"早期信号: {', '.join(breaks)}")
        parts.append("关注但不行动")
    elif level == "medium":
        breaks = [k for k, v in details.items()
                  if v.get("status") in ("break", "confirmed")]
        parts.append(f"中等信号: {', '.join(breaks)}")
        parts.append("建议LLM深度分析")
    elif level == "strong":
        parts.append("强转折信号！多维共振确认")
        parts.append("准备切换策略")

    return "；".join(parts)


def _update_history(old_history, today_str, phase, a, b, c, level):
    """更新历史记录，保留最近30天，去重今天"""
    new_entry = {
        "date": today_str,
        "phase": phase,
        "a": a, "b": b, "c": c,
        "level": level,
    }

    # 去除今天的旧记录（如果重复运行）
    filtered = [h for h in old_history if h.get("date") != today_str]

    # 插入今天的记录到头部
    updated = [new_entry] + filtered

    # 保留最近30天
    log.debug(f"_update_history: {len(old_history)} → {len(updated[:HISTORY_DAYS])} 条")
    return updated[:HISTORY_DAYS]


def _print_summary(sc):
    """打印检测摘要"""
    summary = sc["summary"]
    phase_cn = sc["current_phase_cn"]
    level = summary["signal_level"]

    level_icon = {"none": "🟢", "early": "🟡", "medium": "🟠", "strong": "🔴"}
    icon = level_icon.get(level, "⚪")

    log.info(f"╔══════════════════════════════════════╗")
    log.info(f"║  转折检测 · {sc['date']}          ║")
    log.info(f"╠══════════════════════════════════════╣")
    log.info(f"║  阶段: {phase_cn:<8} (第{sc['phase_day']}天)         ║")
    log.info(f"║  信号: {icon} {level:<8}                  ║")
    log.info(f"║  A组: {summary['a_score']}  B组: {summary['b_score']}  C组: {summary['c_score']}  ║")
    if summary.get("is_bounce"):
        log.info(f"║  ⚠️  技术反弹日（非风格转换）          ║")
    log.info(f"╠══════════════════════════════════════╣")

    # 打印每个break/confirmed
    for group_key in ["group_a", "group_b", "group_c"]:
        group = sc[group_key]
        for dim_key, dim_val in group["details"].items():
            status = dim_val.get("status", "")
            if status in ("break", "confirmed"):
                desc = dim_val.get("desc", "")[:35]
                log.info(f"║  ⚡ {dim_key}: {desc:<28}║")

    log.info(f"╠══════════════════════════════════════╣")
    log.info(f"║  {summary['interpretation'][:36]:<36}║")
    if summary.get("llm_trigger"):
        log.info(f"║  🤖 LLM深度分析已触发               ║")
    log.info(f"╚══════════════════════════════════════╝")


# ══════════════════════════════════════════════════════════════
# 11. 独立运行入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_transition_detector()
