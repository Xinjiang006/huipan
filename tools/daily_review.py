#!/usr/bin/env python3
"""
tools/daily_review.py  —  慧盘每日复盘报告 assembler
输入: static/data/ 目录下的已有 JSON 文件
输出: static/data/review_history.json (滚动 30 天)

用法:
  python tools/daily_review.py                    # 默认读 static/data/，生成当天
  python tools/daily_review.py --date 2026-04-10  # 指定日期
  python tools/daily_review.py --data-dir /path   # 指定数据目录
  python tools/daily_review.py --dry-run           # 只打印不写文件
"""

import argparse
import json
import os
import sys
from datetime import datetime, date

# ── 默认路径 ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "static", "data")

# ── 常量 ──────────────────────────────────────────────────
HISTORY_KEEP_DAYS = 30
INCUBATION_THRESHOLD = 5
ACCELERATION_THRESHOLD = 15


# ══════════════════════════════════════════════════════════
#  数据加载层
# ══════════════════════════════════════════════════════════

def _load_json(path):
    if not os.path.exists(path):
        print(f"  ⚠ 文件不存在: {path}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ 解析失败 {path}: {e}")
        return None


def _load(data_dir, name):
    return _load_json(os.path.join(data_dir, name))


# ══════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════

def _r(val, n=2):
    """safe round"""
    if val is None:
        return None
    try:
        return round(float(val), n)
    except (ValueError, TypeError):
        return None


def _get_regime_today(regime_history, today_str):
    if not regime_history:
        return None
    for rec in regime_history:
        if str(rec.get("date", ""))[:10] == today_str:
            return rec
    return None


# ══════════════════════════════════════════════════════════
#  Assembler 函数
# ══════════════════════════════════════════════════════════

def assemble_market(regime_today, overview):
    """
    指数: regime_history → sh_change_pct / sz_change_pct / cyb_change_pct / csi1000_change_pct
    hs300: overview.cap_indices.large.change_pct
    涨跌/炸板: overview.kpi (权威源)
    volume/regime: regime_history
    """
    r = regime_today or {}
    o = overview or {}
    kpi = o.get("kpi", {})
    cap = o.get("cap_indices", {})

    return {
        "sh": _r(r.get("sh_change_pct")),
        "sz": _r(r.get("sz_change_pct")),
        "cyb": _r(r.get("cyb_change_pct")),
        "csi1000": _r(r.get("csi1000_change_pct")),
        "hs300": _r(cap.get("large", {}).get("change_pct")),
        "volume_total": _r(r.get("volume_total"), 1),
        "volume_rank_30d": r.get("volume_rank_30d"),
        "up_count": kpi.get("up_count") or r.get("up_count"),
        "down_count": kpi.get("down_count") or r.get("down_count"),
        "up_ratio": _r(r.get("up_ratio"), 1),
        "median_change_pct": _r(r.get("median_change_pct")),
        "limit_up": kpi.get("limit_up") or r.get("limit_up"),
        "limit_down": kpi.get("limit_down") or r.get("limit_down"),
        "zha_ban_rate": _r(kpi.get("zha_ban_rate"), 1),
        "lianban_survived": kpi.get("lianban_survived"),
        "lianban_total": kpi.get("lianban_total"),
        "regime_label": r.get("regime_label", "unknown"),
    }


def assemble_regime_context(regime_history):
    recent = (regime_history or [])[:7]
    recent_7d = [{"date": str(r.get("date", ""))[:10],
                  "label": r.get("regime_label", "unknown")} for r in recent]

    current_label = recent_7d[0]["label"] if recent_7d else "unknown"
    streak = 0
    for item in recent_7d:
        if item["label"] == current_label:
            streak += 1
        else:
            break

    last_momentum = None
    momentum_count = 0
    for rec in (regime_history or [])[:30]:
        if rec.get("regime_label") == "momentum":
            momentum_count += 1
            if last_momentum is None:
                last_momentum = str(rec.get("date", ""))[:10]

    return {
        "recent_7d": recent_7d,
        "current_streak": streak,
        "current_streak_label": current_label,
        "last_momentum_date": last_momentum,
        "momentum_count_30d": momentum_count,
    }


def assemble_health(regime_today):
    r = regime_today or {}
    fields = [
        "vwap_bias_median", "intraday_strength_median",
        "change_pct_stdev", "extreme_ratio", "zt_dt_ratio",
        "cap_scissors", "new_high_low_diff",
        "breadth_5d_avg", "volatility_5d",
    ]
    return {f: _r(r.get(f), 3 if "volatility" in f else 2) for f in fields}


def assemble_volume_context(regime_history):
    recent_5 = []
    for rec in (regime_history or [])[:5]:
        recent_5.append(_r(rec.get("volume_total"), 1))
    while len(recent_5) < 5:
        recent_5.append(None)
    recent_5.reverse()  # 最老在前, index 4 = 今天

    today_vol = recent_5[4] if len(recent_5) == 5 else None
    valid = [v for v in recent_5 if v is not None]
    trend = None
    if len(valid) >= 2:
        if valid[-1] < valid[-2] * 0.95:
            trend = "缩量"
        elif valid[-1] > valid[-2] * 1.05:
            trend = "放量"
        else:
            trend = "平量"

    rank = (regime_history[0] if regime_history else {}).get("volume_rank_30d")

    return {
        "today": today_vol,
        "recent_5d": recent_5,
        "trend": trend,
        "rank_30d": rank,
    }


def assemble_hhi_context(intraday_data, review_history_days):
    snapshots = (intraday_data or {}).get("snapshots", [])
    today_close = None
    today_peak = None
    for snap in snapshots:
        hhi = snap.get("realtime_hhi")
        if hhi is not None:
            if today_peak is None or hhi > today_peak:
                today_peak = hhi
            today_close = hhi  # 最后一个 snapshot

    # 从已有 review_history 回填近 4 天
    past_hhi = []
    for pd in (review_history_days or [])[:4]:
        h = (pd.get("hhi_context") or {}).get("today_close")
        past_hhi.append(h)
    while len(past_hhi) < 4:
        past_hhi.append(None)
    past_hhi.reverse()  # 最老在前
    recent_5d = past_hhi + [today_close]

    # percentile_30d
    all_hhi = [
        (d.get("hhi_context") or {}).get("today_close")
        for d in (review_history_days or [])
    ]
    all_hhi = [h for h in all_hhi if h is not None]
    percentile = None
    if len(all_hhi) >= 30 and today_close is not None:
        below = sum(1 for h in all_hhi if h <= today_close)
        percentile = _r(below / len(all_hhi) * 100, 1)

    return {
        "today_close": today_close,
        "today_peak": today_peak,
        "recent_5d_close": recent_5d,
        "percentile_30d": percentile,
    }


def assemble_snapshots(intraday_data):
    """缺失的时间点跳过，不填 null 占位"""
    snapshots = (intraday_data or {}).get("snapshots", [])
    result = []
    for snap in snapshots:
        time_str = snap.get("time") or snap.get("snapshot_time", "")
        if not time_str:
            continue
        sector_dist = snap.get("realtime_sector_dist", {})
        top3 = sorted(sector_dist.items(), key=lambda x: x[1], reverse=True)[:3]
        result.append({
            "time": time_str,
            "up_ratio": _r(snap.get("up_ratio"), 1),
            "median_change_pct": _r(snap.get("median_change_pct")),
            "limit_up": snap.get("limit_up"),
            "hhi": snap.get("realtime_hhi"),
            "top3_sectors": [{"sector": s, "count": c} for s, c in top3],
        })
    return result


def _calc_phase(count_5d):
    today = count_5d[-1] if count_5d else None
    if today is None:
        return "unknown", 0
    phase_day = 0
    for c in reversed(count_5d):
        if c is not None and c >= INCUBATION_THRESHOLD:
            phase_day += 1
        else:
            break
    if today >= ACCELERATION_THRESHOLD:
        return "加速", phase_day
    elif today >= INCUBATION_THRESHOLD:
        return "孵化", phase_day
    else:
        return "观望", phase_day


def _calc_incubation_signal(count_5d):
    if not count_5d:
        return None
    for c in count_5d:
        if c is not None and c >= ACCELERATION_THRESHOLD:
            return "加速"
    consec = 0
    for c in count_5d:
        if c is not None and c >= INCUBATION_THRESHOLD:
            consec += 1
            if consec >= 2:
                return "孵化"
        else:
            consec = 0
    return None


def assemble_sector_summary(intraday_data, review_history_days):
    snapshots = (intraday_data or {}).get("snapshots", [])
    today_dist = {}
    peak_dist = {}
    for snap in snapshots:
        dist = snap.get("realtime_sector_dist", {})
        for sector, count in dist.items():
            if sector not in peak_dist or count > peak_dist[sector]:
                peak_dist[sector] = count
            today_dist[sector] = count

    # 从 review_history 构建近 4 天 sector count (最老在前)
    past_days = (review_history_days or [])[:4]
    past_days_reversed = list(reversed(past_days))  # 最老在前
    past_sector_counts = []
    for pd in past_days_reversed:
        day_map = {}
        for s in pd.get("sector_summary", []):
            day_map[s["sector"]] = s.get("count_close")
        past_sector_counts.append(day_map)
    while len(past_sector_counts) < 4:
        past_sector_counts.insert(0, {})

    result = []
    for sector in sorted(today_dist.keys(), key=lambda s: today_dist[s], reverse=True):
        count_close = today_dist[sector]
        if count_close < 3:
            continue
        count_peak = peak_dist.get(sector, count_close)
        count_5d = [psc.get(sector) for psc in past_sector_counts] + [count_close]

        phase, phase_day = _calc_phase(count_5d)
        incubation_signal = _calc_incubation_signal(count_5d)

        delta_1d = (count_close - count_5d[3]) if count_5d[3] is not None else None
        delta_5d = (count_close - count_5d[0]) if count_5d[0] is not None else None

        result.append({
            "sector": sector,
            "count_close": count_close,
            "count_peak": count_peak,
            "count_5d": count_5d,
            "phase": phase,
            "phase_day": phase_day,
            "delta_1d": delta_1d,
            "delta_5d": delta_5d,
            "incubation_signal": incubation_signal,
        })
    return result


def assemble_reversal_candidates(reversal_watchlist):
    """
    字段映射:
      scores.total → score
      scores (去掉total) → score_detail
      sector_context.in_top100 → in_top100
      triggered: today_pct >= 5%
    返回: (top10_list, total_count)
    """
    wl = reversal_watchlist or {}
    candidates = wl.get("candidates", [])
    total_count = len(candidates)

    result = []
    for c in candidates[:10]:
        scores = c.get("scores", {})
        sc = c.get("sector_context", {})
        today_pct = c.get("today_pct")

        triggered = False
        if today_pct is not None:
            try:
                triggered = float(today_pct) >= 5.0
            except (ValueError, TypeError):
                pass

        result.append({
            "code": c.get("code"),
            "name": c.get("name"),
            "sector": c.get("sector"),
            "score": scores.get("total"),
            "score_detail": {k: v for k, v in scores.items() if k != "total"},
            "today_pct": _r(today_pct),
            "volume_ratio": _r(c.get("volume_ratio")),
            "original_drop": _r(c.get("original_drop")),
            "stop_days": c.get("stop_days"),
            "in_top100": sc.get("in_top100"),
            "triggered_today": triggered,
        })

    return result, total_count


def assemble_t1_momentum(regime_today, regime_history):
    """
    字段映射:
      momentum_avg_return → avg_t1
      momentum_median_return → median_t1
      momentum_matched → count
      momentum_up_count / momentum_matched → win_rate
      picks_date → source_date
    注意: picks_date == return_date 说明 T+1 还没跑
    """
    r = regime_today or {}
    matched = r.get("momentum_matched", 0)
    up_count = r.get("momentum_up_count", 0)
    source_date = str(r.get("picks_date", ""))[:10] or None
    return_date = str(r.get("return_date", ""))[:10]

    if source_date == return_date:
        return {
            "source_date": source_date,
            "count": matched,
            "avg_t1": None,
            "win_rate": None,
            "median_t1": None,
            "note": f"picks_date={source_date} == return_date, T+1尚未计算",
        }

    win_rate = _r(up_count / matched * 100, 1) if matched > 0 else None

    return {
        "source_date": source_date,
        "count": matched,
        "avg_t1": _r(r.get("momentum_avg_return")),
        "win_rate": win_rate,
        "median_t1": _r(r.get("momentum_median_return")),
    }


def assemble_t1_reversal(data_dir, today_str):
    """从昨日 review_history 取 reversal 候选，用今日 watchlist 的 today_pct 验证"""
    review_hist = _load(data_dir, "review_history.json")
    yesterday_candidates = []
    source_date = None

    if review_hist and review_hist.get("days"):
        for day in review_hist["days"]:
            d = day.get("date", "")
            if d != today_str:
                source_date = d
                yesterday_candidates = day.get("reversal_candidates", [])
                break

    empty_buckets = {
        "score_90plus": {"count": 0, "avg_t1": None, "win_rate": None},
        "score_80plus": {"count": 0, "avg_t1": None, "win_rate": None},
        "score_70plus": {"count": 0, "avg_t1": None, "win_rate": None},
        "all":          {"count": 0, "avg_t1": None, "win_rate": None},
    }

    if not yesterday_candidates:
        return {
            "source_date": source_date,
            "candidates_count": 0,
            "results": [],
            "by_score_bucket": empty_buckets,
            "note": f"昨日({source_date}) 无 reversal 候选",
        }

    wl = _load(data_dir, "reversal_watchlist.json") or {}
    current_map = {c.get("code"): c for c in wl.get("candidates", [])}

    results = []
    for c in yesterday_candidates:
        code = c.get("code")
        t1_pct = None
        if code in current_map:
            t1_pct = current_map[code].get("today_pct")
        results.append({
            "code": code,
            "name": c.get("name"),
            "score": c.get("score"),
            "t1_pct": _r(t1_pct),
        })

    def _bucket(items, min_score=0):
        f = [r for r in items if (r.get("score") or 0) >= min_score and r["t1_pct"] is not None]
        if not f:
            return {"count": 0, "avg_t1": None, "win_rate": None}
        avg = sum(r["t1_pct"] for r in f) / len(f)
        win = sum(1 for r in f if r["t1_pct"] > 0) / len(f) * 100
        return {"count": len(f), "avg_t1": _r(avg), "win_rate": _r(win, 1)}

    return {
        "source_date": source_date,
        "candidates_count": len(yesterday_candidates),
        "results": results,
        "by_score_bucket": {
            "score_90plus": _bucket(results, 90),
            "score_80plus": _bucket(results, 80),
            "score_70plus": _bucket(results, 70),
            "all":          _bucket(results, 0),
        },
    }


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

def merge_to_history(existing_history, new_entry):
    days = (existing_history or {}).get("days", [])
    today_str = new_entry["date"]
    days = [d for d in days if d.get("date") != today_str]
    days.insert(0, new_entry)
    days = days[:HISTORY_KEEP_DAYS]
    return {"$schema": "review_history_v1.0", "days": days}


def assemble(data_dir, today_str):
    print(f"═══ 生成复盘报告: {today_str} ═══")
    print(f"  数据目录: {data_dir}")

    regime_history = _load(data_dir, "regime_history.json")
    overview = _load(data_dir, "ashare_overview.json")
    intraday = _load(data_dir, "derived_intraday.json")
    reversal_wl = _load(data_dir, "reversal_watchlist.json")

    # 用 regime_history 的日期，不用 today()
    regime_today = _get_regime_today(regime_history, today_str)
    if not regime_today and regime_history:
        regime_today = regime_history[0]
        actual_date = str(regime_today.get("date", ""))[:10]
        if actual_date != today_str:
            print(f"  ⚠ 指定日期 {today_str} 不在 regime_history, 使用最新 {actual_date}")
            today_str = actual_date

    # 已有 review_history（用于回填 sector_5d / hhi_5d）
    existing = _load(data_dir, "review_history.json")
    existing_days = (existing or {}).get("days", [])

    # reversal
    rev_candidates, rev_total = assemble_reversal_candidates(reversal_wl)

    entry = {
        "date": today_str,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": assemble_market(regime_today, overview),
        "regime_context": assemble_regime_context(regime_history),
        "health": assemble_health(regime_today),
        "volume_context": assemble_volume_context(regime_history),
        "hhi_context": assemble_hhi_context(intraday, existing_days),
        "snapshots": assemble_snapshots(intraday),
        "sector_summary": assemble_sector_summary(intraday, existing_days),
        "reversal_candidates_total": rev_total,
        "reversal_candidates": rev_candidates,
        "t1_verify": {
            "momentum": assemble_t1_momentum(regime_today, regime_history),
            "reversal": assemble_t1_reversal(data_dir, today_str),
        },
    }

    result = merge_to_history(existing, entry)

    # 打印摘要
    m = entry["market"]
    rc = entry["regime_context"]
    print(f"\n  ── 摘要 ──")
    print(f"  Regime: {m.get('regime_label')} (连续{rc.get('current_streak')}天)")
    print(f"  指数: 上证 {m.get('sh')}%  深证 {m.get('sz')}%  创业板 {m.get('cyb')}%  中证1000 {m.get('csi1000')}%  沪深300 {m.get('hs300')}%")
    print(f"  涨跌: {m.get('up_count')}↑ {m.get('down_count')}↓ 比率{m.get('up_ratio')}%")
    print(f"  涨停{m.get('limit_up')} 跌停{m.get('limit_down')} 炸板{m.get('zha_ban_rate')}% 连板{m.get('lianban_survived')}/{m.get('lianban_total')}")
    print(f"  板块: {len(entry['sector_summary'])}个活跃")
    snaps = entry["snapshots"]
    print(f"  快照: {len(snaps)}个时间点 ({', '.join(s['time'] for s in snaps)})")
    print(f"  Reversal候选: {len(entry['reversal_candidates'])}/{rev_total}只")
    t1m = entry["t1_verify"]["momentum"]
    note = t1m.get("note", "")
    if note:
        print(f"  T+1 momentum: {note}")
    else:
        print(f"  T+1 momentum: avg={t1m.get('avg_t1')}% win={t1m.get('win_rate')}% (n={t1m.get('count')})")
    print(f"  历史天数: {len(result['days'])}")

    return result


def main():
    parser = argparse.ArgumentParser(description="慧盘每日复盘报告 assembler")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="报告日期 (默认今天, 非交易日自动回退到最新)")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="数据目录路径")
    parser.add_argument("--output", default=None,
                        help="输出路径 (默认: <data-dir>/review_history.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印摘要不写文件")
    args = parser.parse_args()

    result = assemble(args.data_dir, args.date)

    if args.dry_run:
        print(f"\n  [dry-run] JSON 预览 (前2000字符):")
        print(json.dumps(result["days"][0], ensure_ascii=False, indent=2)[:2000])
    else:
        out = args.output or os.path.join(args.data_dir, "review_history.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n  ✅ 已写入: {out} ({os.path.getsize(out):,} bytes)")


if __name__ == "__main__":
    main()
