"""
慧盘 · regime_daily 重建工具 v5.1
从 archive/spot/*.pkl 重建 DuckDB regime_daily 表 + 导出 regime_history.json

用法：
  cd ~/huipan
  python3 tools/rebuild_regime.py              # 全量重建
  python3 tools/rebuild_regime.py --dry-run    # 只算不写，对比验证
  python3 tools/rebuild_regime.py --skip-db    # 跳过DuckDB，只输出JSON

v5.1修复：
  - 修复 calc_prior_analysis 双重调用bug（旧版第二次用错误参数覆盖正确结果）
  - 修复 calc_limit_counts 使用未过滤df（含北交所）的问题
  - 新增脏数据检测：相邻pkl内容相同时跳过后者（04/06类非交易日）
  - prior分析使用自建日期索引，不依赖archive目录扫描（避免脏pkl偏移T-N）
  - 新增 --dry-run 模式，只计算不写入
  - 新增逐日摘要报告

已知限制（pkl里无法还原的字段，写null）：
  - sh/sz/cyb/csi1000_change_pct（指数涨跌幅）
  - volatility_5d（5日波动率）
  - cap_scissors（大小盘剪刀差）
"""

import os
import sys
import json
import math
import time
import pickle
import hashlib
import argparse
import statistics
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from compute.indicators import (
    calc_next_day_returns,
    calc_next_day_returns_by_tier,
    calc_cap_dist,
    calc_price_dist,
    calc_sector_stats,
    calc_derived_indicators,
    calc_intraday_strength,
    calc_vwap_bias,
    apply_regime_label,
    build_today_chg_map,
    TIERS,
)
from compute.prior_analysis import calc_prior_analysis, build_close_map
from utils.stock_filter import filter_valid, calc_limit_counts, find_limit_up_codes
from data_io.regime_io import (
    DDL,
    _migrate_regime_table,
    save_to_duckdb,
    ARCHIVE_SPOT_DIR,
    DB_PATH,
    HISTORY_PATH,
    DATA_DIR,
)
from collector.ashare_movers import (
    build_picks_from_df,
    load_sector_map,
    load_index_cons_map,
    clean_nan,
)

PICKS_PATH = os.path.join(DATA_DIR, "yesterday_picks.json")


# ══════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════

def list_archive_dates():
    """列出 archive/spot/ 下所有可用日期，升序返回（去重）"""
    if not os.path.isdir(ARCHIVE_SPOT_DIR):
        return []
    dates = set()
    for f in os.listdir(ARCHIVE_SPOT_DIR):
        if f.startswith("spot_") and f.endswith(".pkl"):
            d = f[5:13]
            try:
                ds = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                dates.add(ds)
            except Exception:
                continue
    return sorted(dates)


def load_pkl(date_str):
    """加载指定日期的归档pkl，返回DataFrame"""
    fname = f"spot_{date_str.replace('-', '')}.pkl"
    path = os.path.join(ARCHIVE_SPOT_DIR, fname)
    with open(path, "rb") as f:
        cache = pickle.load(f)
    df = cache.get("df")
    if df is None or len(df) == 0:
        raise ValueError(f"pkl {fname} 内容为空")
    return df


def pkl_fingerprint(df):
    """生成df的指纹用于去重检测（前50只股票的涨跌幅拼接）"""
    col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
    col_code = next((c for c in df.columns if "代码" in c), None)
    if not col_chg or not col_code:
        return ""
    sample = df.head(50)
    text = "|".join(f"{r[col_code]}:{r[col_chg]:.4f}" for _, r in sample.iterrows())
    return hashlib.md5(text.encode()).hexdigest()


def detect_dirty_dates(dates):
    """检测非交易日脏数据：相邻pkl指纹相同则后者为脏数据"""
    clean = []
    skipped = []
    prev_fp = None
    for ds in dates:
        try:
            df = load_pkl(ds)
            fp = pkl_fingerprint(df)
            if prev_fp and fp == prev_fp:
                skipped.append(ds)
                print(f"  ⚠️ 跳过 {ds}：与前一天数据完全相同（非交易日脏数据）")
            else:
                clean.append(ds)
            prev_fp = fp
        except Exception as e:
            print(f"  ⚠️ 跳过 {ds}：加载失败 {e}")
            skipped.append(ds)
    return clean, skipped


def build_sup_from_df(df_valid):
    """从过滤后的df构建supplementary数据"""
    sup = {}
    col_chg = next((c for c in df_valid.columns if "涨跌幅" in c), None)
    col_vol = next((c for c in df_valid.columns if "成交额" in c), None)

    if col_chg:
        chg_series = df_valid[col_chg].dropna().astype(float)
        sup["up_count"] = int((chg_series > 0).sum())
        sup["down_count"] = int((chg_series < 0).sum())
        total = len(chg_series)
        sup["up_ratio"] = round(sup["up_count"] / total * 100, 1) if total else None

    if col_vol:
        try:
            vol_sum = df_valid[col_vol].dropna().astype(float).sum()
            sup["volume_total"] = round(vol_sum / 1e8, 2)
        except Exception:
            sup["volume_total"] = None

    lu, ld = calc_limit_counts(df_valid)
    sup["limit_up"] = lu
    sup["limit_down"] = ld
    sup["sh_change_pct"] = None
    sup["sz_change_pct"] = None
    sup["cyb_change_pct"] = None
    sup["csi1000_change_pct"] = None
    return sup


def calc_health_from_history(df_valid, sup, history_rows):
    """用已处理的历史rows计算健康指标（不依赖DuckDB）
    v5.3: vwap_bias_median + intraday_strength_median 替换 zt_dt_ratio + new_high_low_diff
    """
    result = {
        "breadth_5d_avg": None,
        "vwap_bias_median": None,
        "intraday_strength_median": None,
        "volatility_5d": None,
    }

    # v5.3: 日内强度 + VWAP偏离
    if df_valid is not None:
        result["vwap_bias_median"] = calc_vwap_bias(df_valid)
        result["intraday_strength_median"] = calc_intraday_strength(df_valid)

    recent = history_rows[-5:] if len(history_rows) >= 2 else []
    ratios = [r.get("up_ratio") for r in recent if r.get("up_ratio") is not None]
    if sup.get("up_ratio") is not None:
        ratios.append(sup["up_ratio"])
    if len(ratios) >= 2:
        result["breadth_5d_avg"] = round(sum(ratios) / len(ratios), 2)

    return result


def save_final_picks(picks_data):
    """将最后一天的picks写入yesterday_picks.json"""
    data = {
        "date": picks_data["date"],
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_final": True,
        "top100_gainers": picks_data["gainers"],
        "top100_losers": picks_data["losers"],
    }
    data = clean_nan(data)
    with open(PICKS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ yesterday_picks.json 已更新为 {picks_data['date']} 的数据")


def _calc_prior_from_loaded(i, dates, all_dfs, df_today):
    """
    从预加载的pkl数据计算前置分析，使用自建日期索引。
    T-1: dates[i-1]
    T-4: dates[i-4]（prev3 = T-1 close / T-4 close）
    T-6: dates[i-6]（prev5 = T-1 close / T-6 close）
    """
    if i < 1:
        print(f"  ℹ️ 前置分析: 首日，无T-1数据")
        return _empty_prior()

    t1_date = dates[i - 1]
    t1_df = all_dfs[t1_date]
    chg_t1 = build_today_chg_map(t1_df)
    close_t1 = build_close_map(t1_df)

    t4_idx = i - 4
    if t4_idx >= 0:
        close_t4 = build_close_map(all_dfs[dates[t4_idx]])
        t4_label = dates[t4_idx]
    else:
        close_t4 = close_t1
        t4_label = t1_date + "(降级)"

    t6_idx = i - 6
    if t6_idx >= 0:
        close_t6 = build_close_map(all_dfs[dates[t6_idx]])
        t6_label = dates[t6_idx]
    else:
        close_t6 = close_t4
        t6_label = t4_label + "(降级)"

    print(f"  ℹ️ 前置: T-1={t1_date}, T-4={t4_label}, T-6={t6_label}")
    return calc_prior_analysis(df_today, chg_t1, close_t1, close_t4, close_t6)


def _empty_prior():
    empty = {}
    for prefix in ["gn", "ls"]:
        for win in [1, 3, 5]:
            for metric in ["same", "avg", "med", "strong"]:
                empty[f"{prefix}_prev{win}_{metric}"] = None
    return empty


# ══════════════════════════════════════════════
# 主重建逻辑
# ══════════════════════════════════════════════

def rebuild(dry_run=False, skip_db=False):
    print("=" * 60)
    print("慧盘 · regime_daily 重建工具 v5.1")
    if dry_run:
        print("🔍 DRY-RUN 模式：只计算不写入")
    print("=" * 60)

    # 1. 扫描归档
    raw_dates = list_archive_dates()
    if not raw_dates:
        print("❌ archive/spot/ 下无pkl文件，退出")
        return

    print(f"📦 发现 {len(raw_dates)} 个归档")

    # 2. 脏数据检测
    print("\n🔍 脏数据检测...")
    dates, skipped = detect_dirty_dates(raw_dates)
    print(f"   有效: {len(dates)}天, 跳过: {len(skipped)}天 {skipped}")

    if not dates:
        print("❌ 无有效交易日数据，退出")
        return

    # 3. 加载配置
    sector_map = load_sector_map()
    cons_map = load_index_cons_map()

    if not skip_db and not dry_run:
        if not os.path.exists(DB_PATH):
            print(f"❌ DuckDB不存在: {DB_PATH}，退出")
            return
        import duckdb
        con = duckdb.connect(DB_PATH)
        con.execute("DROP TABLE IF EXISTS regime_daily")
        print("🗑️  regime_daily 表已DROP")
        con.execute(DDL)
        _migrate_regime_table(con)
        con.close()
        print("✅ regime_daily 表已用最新DDL重建\n")

    # 4. 预加载所有pkl
    print("📂 预加载所有pkl...")
    all_dfs = {}
    all_raw = {}
    for ds in dates:
        try:
            raw = load_pkl(ds)
            valid = filter_valid(raw, exclude_bj=True, exclude_new=False, exclude_suspended=True)
            all_raw[ds] = raw
            all_dfs[ds] = valid
            print(f"   {ds}: {len(raw)}只 → {len(valid)}只(过滤后)")
        except Exception as e:
            print(f"   {ds}: ❌ 加载失败 {e}")

    dates = [d for d in dates if d in all_dfs]
    print(f"\n✅ 已加载 {len(dates)} 天: {dates[0]} ~ {dates[-1]}\n")

    # 5. 逐天处理
    prev_picks = None
    history_rows = []
    success_count = 0
    summary_rows = []

    for i, date_str in enumerate(dates):
        print(f"{'─' * 50}")
        print(f"📅 [{i+1}/{len(dates)}] {date_str}")

        df_valid = all_dfs[date_str]
        df_raw = all_raw[date_str]

        # picks
        today_gainers, today_losers = build_picks_from_df(df_raw, sector_map, cons_map)
        if today_gainers is None:
            print(f"  ❌ picks提取失败，跳过")
            continue
        print(f"  ✅ picks: 涨{len(today_gainers)}/跌{len(today_losers)}")

        # T+1
        mom_returns = {"avg": None, "median": None, "up_count": 0, "matched": 0}
        rev_returns = {"avg": None, "median": None, "up_count": 0, "matched": 0}
        mom_tier = {t: {"avg": None, "median": None, "up_count": 0, "n": 0} for t in TIERS}
        rev_tier = {t: {"avg": None, "median": None, "up_count": 0, "n": 0} for t in TIERS}
        picks_date = None
        return_date = None

        if prev_picks is not None:
            chg_map = build_today_chg_map(df_valid)
            picks_date = prev_picks["date"]
            return_date = date_str
            mom_returns = calc_next_day_returns(prev_picks["gainers"], chg_map)
            rev_returns = calc_next_day_returns(prev_picks["losers"], chg_map)
            mom_tier = calc_next_day_returns_by_tier(prev_picks["gainers"], chg_map)
            rev_tier = calc_next_day_returns_by_tier(prev_picks["losers"], chg_map)
            wr = round(mom_returns["up_count"] / mom_returns["matched"] * 100, 1) if mom_returns["matched"] else None
            print(f"  ✅ T+1(picks={picks_date}): wr={wr}% avg={mom_returns['avg']}%")

        # 分布
        gainer_cap = calc_cap_dist(today_gainers)
        loser_cap = calc_cap_dist(today_losers)
        gainer_price = calc_price_dist(today_gainers)
        loser_price = calc_price_dist(today_losers)
        sector_stats = calc_sector_stats(today_gainers, today_losers)

        # 补充数据
        sup = build_sup_from_df(df_valid)

        # 衍生指标
        yd_lu_codes = find_limit_up_codes(all_dfs[dates[i-1]], exclude_bj=True) if i > 0 else None
        derived = calc_derived_indicators(df_valid, today_gainers, today_losers, sup, yd_lu_codes)

        # 健康
        health = calc_health_from_history(df_valid, sup, history_rows)

        # 前置分析
        prior_result = _calc_prior_from_loaded(i, dates, all_dfs, df_valid)

        # 组装record
        record = {
            "date": date_str,
            "picks_date": picks_date,
            "return_date": return_date,
            "volume_total": sup.get("volume_total"),
            "volume_rank_30d": None,
            "limit_up": sup.get("limit_up"),
            "limit_down": sup.get("limit_down"),
            "up_count": sup.get("up_count"),
            "down_count": sup.get("down_count"),
            "up_ratio": sup.get("up_ratio"),
            "sh_change_pct": None,
            "sz_change_pct": None,
            "cyb_change_pct": None,
            "csi1000_change_pct": None,
            "momentum_avg_return": mom_returns["avg"],
            "momentum_median_return": mom_returns["median"],
            "momentum_up_count": mom_returns["up_count"],
            "momentum_matched": mom_returns["matched"],
            "reversion_avg_return": rev_returns["avg"],
            "reversion_median_return": rev_returns["median"],
            "reversion_up_count": rev_returns["up_count"],
            "reversion_matched": rev_returns["matched"],
            "gainer_micro": gainer_cap.get("微盘", 0),
            "gainer_small": gainer_cap.get("小盘", 0),
            "gainer_mid": gainer_cap.get("中盘", 0),
            "gainer_large": gainer_cap.get("大盘", 0),
            "loser_micro": loser_cap.get("微盘", 0),
            "loser_small": loser_cap.get("小盘", 0),
            "loser_mid": loser_cap.get("中盘", 0),
            "loser_large": loser_cap.get("大盘", 0),
            "gainer_p0_10": gainer_price.get("0-10", 0),
            "gainer_p10_30": gainer_price.get("10-30", 0),
            "gainer_p30_50": gainer_price.get("30-50", 0),
            "gainer_p50_100": gainer_price.get("50-100", 0),
            "gainer_p100p": gainer_price.get("100+", 0),
            "loser_p0_10": loser_price.get("0-10", 0),
            "loser_p10_30": loser_price.get("10-30", 0),
            "loser_p30_50": loser_price.get("30-50", 0),
            "loser_p50_100": loser_price.get("50-100", 0),
            "loser_p100p": loser_price.get("100+", 0),
            **sector_stats,
            **derived,
            **health,
            **prior_result,
        }

        for prefix, tier_data in [("mom", mom_tier), ("rev", rev_tier)]:
            for tier in TIERS:
                td = tier_data[tier]
                record[f"{prefix}_{tier}_avg"] = td["avg"]
                record[f"{prefix}_{tier}_median"] = td["median"]
                record[f"{prefix}_{tier}_up"] = td["up_count"]
                record[f"{prefix}_{tier}_n"] = td["n"]

        record["regime_label"] = apply_regime_label(record)
        print(f"  ✅ regime: {record['regime_label']}")

        if not dry_run and not skip_db:
            save_to_duckdb(record)

        success_count += 1
        history_rows.append(record)

        wr = round(mom_returns["up_count"] / mom_returns["matched"] * 100, 1) if mom_returns.get("matched") else None
        summary_rows.append({
            "date": date_str, "lu": sup.get("limit_up"),
            "med": derived.get("median_change_pct"), "wr": wr,
            "avg_g": mom_returns.get("avg"), "p1": prior_result.get("gn_prev1_same"),
            "regime": record["regime_label"],
        })

        prev_picks = {"date": date_str, "gainers": today_gainers, "losers": today_losers}

    # 6. 收尾
    print(f"\n{'=' * 60}")
    print(f"✅ 重建完成: {success_count}/{len(dates)} 天")

    if prev_picks and not dry_run:
        save_final_picks(prev_picks)

    print(f"\n{'date':<12} {'lu':>4} {'med':>7} {'wr':>6} {'avg_g':>7} {'p1':>5} {'regime':<16}")
    print("─" * 62)
    for s in summary_rows:
        wr_s = f"{s['wr']:.1f}" if s['wr'] is not None else "N/A"
        avg_s = f"{s['avg_g']:.2f}" if s['avg_g'] is not None else "N/A"
        p1_s = f"{s['p1']:.0f}" if s['p1'] is not None else "N/A"
        med_s = f"{s['med']:.2f}" if s['med'] is not None else "N/A"
        print(f"{s['date']:<12} {s['lu'] or 0:>4} {med_s:>7} {wr_s:>6} {avg_s:>7} {p1_s:>5} {s['regime']:<16}")

    print(f"\n📋 pkl无法还原: index_pct, cap_scissors, volatility_5d")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="慧盘 · regime_daily 重建 v5.1")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写入")
    parser.add_argument("--skip-db", action="store_true", help="跳过DuckDB")
    args = parser.parse_args()
    rebuild(dry_run=args.dry_run, skip_db=args.skip_db)
