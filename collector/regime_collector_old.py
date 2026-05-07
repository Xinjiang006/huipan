"""
慧盘 · Regime日度计算模块
v4.4 — 对齐derived_intraday计算逻辑 + 修复盘中数据污染

职责：
  1. 读 yesterday_picks.json（T日涨跌Top100）
  2. 读 .spot_cache.pkl（T+1全市场涨跌幅）
  3. 计算T+1次日收益（整体 + 四档拆分） + 当日分布指标 + regime_label
  4. 计算14个衍生/健康指标
  5. 计算前置分析（回溯前1/3/5日，24个字段）
  6. 写入 DuckDB regime_daily 表
  7. 导出最近30条到 static/data/regime_history.json

可独立运行：python3 collector/regime_collector.py
可被import：from collector.regime_collector import collect_regime

v4.4变更（对齐derived_intraday计算逻辑）：
  - calc_derived_indicators() 排除北交所（bj开头），与derived_intraday一致
  - 涨停溢价率改用昨日pkl中≥9.8%真涨停股，不再用Top100涨幅股
  - calc_health_indicators() breadth_5d_avg/volatility_5d 查询排除当天（防盘中数据污染）
  - new_high_low_diff 优先从 new_high_low.json 读取（16:40延迟统计后的准确值）

v4.3变更：
  - load_supplementary_data() 指数null时从derived_intraday.json补充（腾讯fallback源）
  - derived_intraday.json为snapshots数组结构，取最后一个snapshot
  - 修复 regime_history.json 中 sh_change_pct / cap_scissors 长期为null的问题

v3.10变更（前置分析）：
  - calc_prior_analysis() 新增，回溯archive/spot/pkl计算24个字段
  - DDL + 迁移 新增24列（gn_prev{1,3,5}_{same,avg,med,strong} + ls同）
  - PRIOR_COLUMNS 列表，ALL_NEW_COLUMNS 更新

v3.10变更（板块分布）：
  - calc_sector_stats() 新增 sector_dist_gainers/losers（完整板块分布dict）
  - DDL + 迁移 新增2个JSON列
  - 前端图E气泡图 / 图F集中度趋势 的数据源

v3.8.1变更：
  - calc_next_day_returns / calc_next_day_returns_by_tier / calc_derived_indicators
    排除is_new=True的新股（N/C开头，无涨跌幅限制，扭曲T+1统计）

v3.7.1变更：
  - calc_derived_indicators() 新增6字段：标准差/集中度/极端比/高价股×3
  - DDL扩展14列（衍生10 + 健康4）
"""

import json
import os
import math
import time
import pickle
import statistics
from datetime import datetime, date

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
CACHE_PATH = os.path.join(DATA_DIR, ".spot_cache.pkl")
PICKS_PATH = os.path.join(DATA_DIR, "yesterday_picks.json")
HISTORY_PATH = os.path.join(DATA_DIR, "regime_history.json")
DB_PATH = os.path.join(BASE_DIR, "data", "huipan.duckdb")
ARCHIVE_SPOT_DIR = os.path.join(DATA_DIR, "archive", "spot")

# 市值档位（与 ashare_movers.py CAP_LABEL_MAP 对齐）
TIERS = ["micro", "small", "mid", "large"]
TIER_LABEL_MAP = {"微盘": "micro", "小盘": "small", "中盘": "mid", "大盘": "large"}


# ══════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════

def load_yesterday_picks():
    """读取T日Top100涨跌榜"""
    if not os.path.exists(PICKS_PATH):
        print(f"  ⚠️ yesterday_picks.json不存在，跳过regime计算")
        return None

    try:
        with open(PICKS_PATH, "r", encoding="utf-8") as f:
            picks = json.load(f)

        if not picks.get("is_final"):
            print(f"  ⚠️ yesterday_picks是盘中快照(is_final=False)，跳过")
            return None

        gainers = picks.get("top100_gainers", [])
        losers = picks.get("top100_losers", [])
        picks_date = picks.get("date", "未知")

        print(f"  ✅ picks已加载（{picks_date}，涨{len(gainers)}/跌{len(losers)}）")
        return picks

    except Exception as e:
        print(f"  ❌ picks加载失败: {e}")
        return None


def load_spot_cache(max_retry=3, retry_interval=5):
    """读取全市场行情缓存（带重试）"""
    for attempt in range(1, max_retry + 1):
        try:
            with open(CACHE_PATH, "rb") as f:
                cache = pickle.load(f)
            df = cache.get("df")
            if df is not None and len(df) > 0:
                age = time.time() - cache.get("time", 0)
                print(f"  ✅ pkl已加载（{len(df)}只，{age:.0f}s前）")
                return df
            raise ValueError("pkl内容为空")
        except Exception as e:
            print(f"  ⚠️ pkl读取第{attempt}次失败: {e}")
            if attempt < max_retry:
                time.sleep(retry_interval)

    print(f"  ❌ pkl读取失败（已重试{max_retry}次）")
    return None


def build_today_chg_map(df):
    """
    从全市场df提取 code → today_change_pct 映射
    用于查询T日Top100今日（T+1）的涨跌幅
    """
    col_code = next((c for c in df.columns if "代码" in c), None)
    col_chg  = next((c for c in df.columns if "涨跌幅" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)

    if not col_code or not col_chg:
        return {}

    valid = df[df[col_price] > 0].copy() if col_price else df.copy()
    valid["_code"] = valid[col_code].str.replace(r"^[a-z]{2}", "", regex=True)

    chg_map = {}
    for _, row in valid.iterrows():
        try:
            chg = float(row[col_chg])
            if not math.isnan(chg):
                chg_map[row["_code"]] = chg
        except Exception:
            continue

    return chg_map


def calc_limit_counts(df):
    """
    v3.8: 从spot cache自算涨停/跌停数，消除对ashare_overview.json的执行顺序依赖。
    逻辑与 ashare_overview.py 第90-110行一致。
    """
    col_code = next((c for c in df.columns if "代码" in c), None)
    col_chg  = next((c for c in df.columns if "涨跌幅" in c), None)
    col_name = next((c for c in df.columns if "名称" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)

    if not col_code or not col_chg:
        return 0, 0

    valid = df[df[col_price] > 0].copy() if col_price else df.copy()
    limit_up = 0
    limit_down = 0

    for _, row in valid.iterrows():
        try:
            code = str(row[col_code])
            chg = float(row[col_chg])
            name = str(row[col_name]) if col_name else ""

            if math.isnan(chg):
                continue

            # ST/北交所/创业板科创板 阈值不同
            if "ST" in name or "st" in name:
                threshold = 4.9
            elif code.startswith(("bj",)):
                threshold = 29.5
            elif code.startswith(("sz30", "sh68")):
                threshold = 19.5
            else:
                threshold = 9.9

            if chg >= threshold:
                limit_up += 1
            elif chg <= -threshold:
                limit_down += 1
        except Exception:
            continue

    return limit_up, limit_down


# ══════════════════════════════════════════════
# 2. T+1 次日收益计算
# ══════════════════════════════════════════════

def calc_next_day_returns(picks_list, chg_map):
    """
    计算picks列表中股票在T+1日的收益统计
    v3.8.1: 排除is_new=True的新股（N/C开头，无涨跌幅限制）
    返回 dict: avg, median, up_count, matched
    """
    returns = []
    for stock in picks_list:
        if stock.get("is_new"):
            continue
        code = stock.get("code", "")
        chg = chg_map.get(code)
        if chg is not None:
            returns.append(chg)

    if not returns:
        return {"avg": None, "median": None, "up_count": 0, "matched": 0}

    avg = round(sum(returns) / len(returns), 3)
    med = round(statistics.median(returns), 3)
    up_count = sum(1 for r in returns if r > 0)

    return {
        "avg": avg,
        "median": med,
        "up_count": up_count,
        "matched": len(returns),
    }


def calc_next_day_returns_by_tier(picks_list, chg_map):
    """
    v3.6: 按市值四档分别计算T+1次日收益
    返回 dict: { "micro": {avg, median, up_count, n}, "small": {...}, ... }
    """
    tier_stocks = {t: [] for t in TIERS}
    for stock in picks_list:
        if stock.get("is_new"):
            continue
        cap_label = stock.get("cap_label", "微盘")
        tier_key = TIER_LABEL_MAP.get(cap_label, "micro")
        tier_stocks[tier_key].append(stock)

    result = {}
    for tier in TIERS:
        stocks = tier_stocks[tier]
        returns = []
        for s in stocks:
            chg = chg_map.get(s.get("code", ""))
            if chg is not None:
                returns.append(chg)

        if returns:
            result[tier] = {
                "avg": round(sum(returns) / len(returns), 3),
                "median": round(statistics.median(returns), 3),
                "up_count": sum(1 for r in returns if r > 0),
                "n": len(returns),
            }
        else:
            result[tier] = {"avg": None, "median": None, "up_count": 0, "n": 0}

    return result


# ══════════════════════════════════════════════
# 3. 当日分布计算
# ══════════════════════════════════════════════

def calc_cap_dist(picks_list):
    """市值档位分布：微/小/中/大各多少只"""
    dist = {"微盘": 0, "小盘": 0, "中盘": 0, "大盘": 0, "未知": 0}
    for s in picks_list:
        label = s.get("cap_label", "未知")
        dist[label] = dist.get(label, 0) + 1
    return dist


def calc_price_dist(picks_list):
    """股价档位分布：0-10/10-30/30-50/50-100/100+各多少只"""
    dist = {"0-10": 0, "10-30": 0, "30-50": 0, "50-100": 0, "100+": 0, "未知": 0}
    for s in picks_list:
        label = s.get("price_label", "未知")
        dist[label] = dist.get(label, 0) + 1
    return dist


def calc_sector_stats(gainers, losers):
    """板块统计"""
    from collections import Counter

    def count_sectors(picks_list):
        sectors = [s.get("sector", "未知") for s in picks_list if s.get("sector") != "未知"]
        return Counter(sectors)

    gainer_sectors = count_sectors(gainers)
    loser_sectors  = count_sectors(losers)

    gainer_top5 = set(s for s, _ in gainer_sectors.most_common(5))
    loser_top5  = set(s for s, _ in loser_sectors.most_common(5))
    overlap = len(gainer_top5 & loser_top5)

    sector_count_gainers = len(gainer_sectors)
    sector_count_losers  = len(loser_sectors)

    top_gainer_sectors = [s for s, cnt in gainer_sectors.most_common() if cnt >= 5]
    top_loser_sectors  = [s for s, cnt in loser_sectors.most_common() if cnt >= 5]

    micro_count = sum(1 for s in gainers if s.get("cap_label") == "微盘")
    micro_ratio = round(micro_count / len(gainers) * 100, 1) if gainers else 0

    return {
        "sector_count_gainers": sector_count_gainers,
        "sector_count_losers":  sector_count_losers,
        "sector_overlap":       overlap,
        "top_gainer_sectors":   top_gainer_sectors,
        "top_loser_sectors":    top_loser_sectors,
        "sector_dist_gainers":  dict(gainer_sectors),   # v3.10: 完整分布 {"电力设备":15, ...}
        "sector_dist_losers":   dict(loser_sectors),    # v3.10: 完整分布 {"房地产":18, ...}
        "micro_cap_ratio_gainer": micro_ratio,
    }


# ══════════════════════════════════════════════
# 3.5 v3.7新增：衍生指标 + 市场健康指标
# ══════════════════════════════════════════════

def _find_yesterday_limit_up_codes():
    """v4.4: 从昨日归档pkl中找真涨停股代码（涨跌幅≥9.8%，排除ST和北交所）
    与 derived_intraday.py _calc_zt_premium() 逻辑对齐
    返回: set of 6位代码，或空set
    """
    if not os.path.isdir(ARCHIVE_SPOT_DIR):
        return set()

    today_str = datetime.now().strftime("%Y%m%d")
    # 找最近一个交易日的归档pkl（严格早于今天）
    pkls = sorted(os.listdir(ARCHIVE_SPOT_DIR), reverse=True)
    yd_path = None
    for f in pkls:
        if f.startswith("spot_") and f.endswith(".pkl"):
            pkl_date = f[5:13]
            if pkl_date < today_str:
                yd_path = os.path.join(ARCHIVE_SPOT_DIR, f)
                break

    if yd_path is None:
        print("  ⚠️ 溢价率: 无昨日归档pkl")
        return set()

    try:
        with open(yd_path, "rb") as f:
            cache = pickle.load(f)
        yd_df = cache.get("df")
        if yd_df is None or len(yd_df) == 0:
            return set()

        col_code = next((c for c in yd_df.columns if "代码" in c), None)
        col_chg = next((c for c in yd_df.columns if "涨跌幅" in c), None)
        col_name = next((c for c in yd_df.columns if "名称" in c), None)
        col_price = next((c for c in yd_df.columns if "最新价" in c), None)

        if not col_code or not col_chg:
            return set()

        valid = yd_df.copy()
        if col_price:
            valid = valid[valid[col_price] > 0]

        codes = set()
        for _, row in valid.iterrows():
            try:
                code_raw = str(row[col_code])
                chg = float(row[col_chg])
                name = str(row[col_name]) if col_name else ""
                # 排除ST
                if "ST" in name or "st" in name:
                    continue
                # 排除北交所
                if code_raw.startswith("bj"):
                    continue
                if chg >= 9.8:
                    code = code_raw.replace("sh", "").replace("sz", "").replace("bj", "")
                    codes.add(code)
            except (ValueError, TypeError):
                continue

        print(f"  ✅ 昨日涨停: {len(codes)}只 (from {os.path.basename(yd_path)})")
        return codes

    except Exception as e:
        print(f"  ⚠️ 昨日涨停读取失败: {e}")
        return set()


def calc_derived_indicators(df, gainers, losers, sup):
    """
    v3.7：区域3指标（零新增API，全从df计算）
    v3.7原有4个：zt_premium_avg / cap_scissors / median_change_pct / volume_price_ratio
    v3.7.1新增6个：change_pct_stdev / volume_concentration / extreme_ratio
                   high_price_count / high_price_avg_chg / high_price_up_count
    """
    result = {
        "zt_premium_avg": None,
        "cap_scissors": None,
        "median_change_pct": None,
        "volume_price_ratio": None,
        "change_pct_stdev": None,
        "volume_concentration": None,
        "extreme_ratio": None,
        "high_price_count": None,
        "high_price_avg_chg": None,
        "high_price_up_count": None,
    }

    if df is None:
        return result

    col_code  = next((c for c in df.columns if "代码" in c), None)
    col_chg   = next((c for c in df.columns if "涨跌幅" in c), None)
    col_open  = next((c for c in df.columns if "今开" in c), None)
    col_close = next((c for c in df.columns if "昨收" in c), None)
    col_vol   = next((c for c in df.columns if "成交额" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)

    # 清洗：只保留有效交易（最新价>0 + 涨跌幅非空 + 排除北交所）
    # v4.4: 排除北交所（bj开头，±30%涨跌幅限制，扭曲极端比等指标），与derived_intraday一致
    valid = df.copy()
    if col_price:
        valid = valid[valid[col_price] > 0]
    if col_chg:
        valid = valid[valid[col_chg].notna()]
    if col_code:
        valid = valid[~valid[col_code].str.startswith("bj")]
        valid["_code"] = valid[col_code].str.replace(r"^[a-z]{2}", "", regex=True)

    # ── 1. 涨停溢价率（v4.4: 改用昨日真涨停股，与derived_intraday一致） ──
    # 公式：昨日涨跌幅≥9.8%的个股，今日 (今开-昨收)/昨收 的均值
    # 排除ST和北交所
    if col_open and col_close and col_code:
        premiums = []
        # 从昨日归档pkl找真涨停股
        yd_codes = _find_yesterday_limit_up_codes()
        if yd_codes:
            for _, row in valid.iterrows():
                if row.get("_code") in yd_codes:
                    try:
                        open_p = float(row[col_open])
                        prev_c = float(row[col_close])
                        if prev_c > 0 and open_p > 0:
                            premiums.append((open_p - prev_c) / prev_c * 100)
                    except (ValueError, TypeError):
                        continue
        if premiums:
            result["zt_premium_avg"] = round(sum(premiums) / len(premiums), 3)
            print(f"  ✅ 溢价率: {result['zt_premium_avg']}% ({len(premiums)}只涨停匹配)")
        elif yd_codes:
            print(f"  ⚠️ 溢价率: 昨日{len(yd_codes)}只涨停，今日无匹配")

    # ── 2. 大小盘剪刀差 ──
    sh = sup.get("sh_change_pct")
    csi = sup.get("csi1000_change_pct")
    if sh is not None and csi is not None:
        result["cap_scissors"] = round(sh - csi, 3)
        print(f"  ✅ 剪刀差: {result['cap_scissors']}% (沪深300{sh}% - 中证1000{csi}%)")

    # ── 3. 全市场涨跌幅中位数 ──
    if col_chg:
        try:
            chg_vals = valid[col_chg].dropna().astype(float).tolist()
            chg_vals = [v for v in chg_vals if not math.isnan(v)]
            if chg_vals:
                result["median_change_pct"] = round(statistics.median(chg_vals), 3)
                print(f"  ✅ 中位数涨幅: {result['median_change_pct']}% ({len(chg_vals)}只)")
        except Exception as e:
            print(f"  ⚠️ 中位数计算失败: {e}")

    # ── 4. 量价配合度 ──
    if col_vol and col_chg and col_code:
        try:
            # 排序取涨幅/跌幅Top100
            sorted_df = valid.sort_values(col_chg, ascending=False)
            top100_up = sorted_df.head(100)
            top100_dn = sorted_df.tail(100)

            avg_vol_up = top100_up[col_vol].astype(float).mean()
            avg_vol_dn = top100_dn[col_vol].astype(float).mean()

            if avg_vol_dn > 0:
                result["volume_price_ratio"] = round(avg_vol_up / avg_vol_dn, 3)
                print(f"  ✅ 量价配合: {result['volume_price_ratio']}x")
        except Exception as e:
            print(f"  ⚠️ 量价配合计算失败: {e}")

    # ── 5. 全市场涨跌幅标准差（分化程度）──
    if col_chg:
        try:
            chg_vals = valid[col_chg].dropna().astype(float).tolist()
            chg_vals = [v for v in chg_vals if not math.isnan(v)]
            if len(chg_vals) >= 2:
                result["change_pct_stdev"] = round(statistics.stdev(chg_vals), 3)
                print(f"  ✅ 涨跌幅标准差: {result['change_pct_stdev']}%")
        except Exception as e:
            print(f"  ⚠️ 标准差计算失败: {e}")

    # ── 6. 成交额集中度（Top10占比）──
    if col_vol:
        try:
            vol_vals = valid[col_vol].dropna().astype(float)
            vol_vals = vol_vals[vol_vals > 0]
            if len(vol_vals) >= 10:
                total_vol = vol_vals.sum()
                top10_vol = vol_vals.nlargest(10).sum()
                if total_vol > 0:
                    result["volume_concentration"] = round(top10_vol / total_vol * 100, 2)
                    print(f"  ✅ 成交额集中度: {result['volume_concentration']}%")
        except Exception as e:
            print(f"  ⚠️ 集中度计算失败: {e}")

    # ── 7. 极端涨跌比（>5% vs <-5%）──
    if col_chg:
        try:
            chg_series = valid[col_chg].dropna().astype(float)
            up5 = int((chg_series > 5).sum())
            dn5 = int((chg_series < -5).sum())
            result["extreme_ratio"] = round(up5 / max(dn5, 1), 2)
            print(f"  ✅ 极端涨跌比: {result['extreme_ratio']}x ({up5}涨>{dn5}跌)")
        except Exception as e:
            print(f"  ⚠️ 极端比计算失败: {e}")

    # ── 8/9/10. 高价股（>100元）──
    if col_price and col_chg:
        try:
            hp = valid[valid[col_price].astype(float) > 100].copy()
            hp_count = len(hp)
            result["high_price_count"] = hp_count
            if hp_count > 0:
                hp_chg = hp[col_chg].astype(float)
                result["high_price_avg_chg"] = round(hp_chg.mean(), 3)
                result["high_price_up_count"] = int((hp_chg > 0).sum())
                print(f"  ✅ 高价股: {hp_count}只, avg={result['high_price_avg_chg']}%, 上涨{result['high_price_up_count']}")
            else:
                result["high_price_avg_chg"] = None
                result["high_price_up_count"] = 0
                print(f"  ℹ️ 高价股: 0只")
        except Exception as e:
            print(f"  ⚠️ 高价股计算失败: {e}")

    return result


def calc_health_indicators(sup):
    """
    v3.7：区域4四格指标
    - breadth_5d_avg:    涨跌比5日均线（查DuckDB历史）
    - zt_dt_ratio:       涨停/跌停比
    - new_high_low_diff: 新高-新低差值（ashare_overview.json）
    - volatility_5d:     5日波动率（查DuckDB历史）
    """
    result = {
        "breadth_5d_avg": None,
        "zt_dt_ratio": None,
        "new_high_low_diff": None,
        "volatility_5d": None,
    }

    # ── 1. 涨停/跌停比 ──
    lu = sup.get("limit_up")
    ld = sup.get("limit_down")
    if lu is not None and ld is not None and ld > 0:
        result["zt_dt_ratio"] = round(lu / ld, 2)
    elif lu is not None and ld == 0:
        result["zt_dt_ratio"] = float(lu)  # 无跌停时用涨停数
    print(f"  ✅ 涨跌停比: {result['zt_dt_ratio']}x ({lu}涨停/{ld}跌停)")

    # ── 2. 新高-新低差 ──
    # v4.4: 优先从 new_high_low.json 读取年新高/年新低（16:40延迟统计后的准确值）
    #        new_high_low.json 结构: today.high_year.total / today.low_year.total
    #        fallback到 ashare_overview.json 的月新高/月新低（15:10时THS被跳过，值可能是旧的）
    nhl_path = os.path.join(DATA_DIR, "new_high_low.json")
    overview_path = os.path.join(DATA_DIR, "ashare_overview.json")
    nhl_loaded = False

    if os.path.exists(nhl_path):
        try:
            with open(nhl_path, "r", encoding="utf-8") as f:
                nhl_data = json.load(f)
            today = nhl_data.get("today", {})
            hi = today.get("high_year", {}).get("total", 0)
            lo = today.get("low_year", {}).get("total", 0)
            if hi > 0 or lo > 0:
                result["new_high_low_diff"] = hi - lo
                print(f"  ✅ 新高-新低: {result['new_high_low_diff']} (年高{hi}/年低{lo}, from new_high_low.json)")
                nhl_loaded = True
        except Exception as e:
            print(f"  ⚠️ new_high_low.json读取失败: {e}")

    if not nhl_loaded and os.path.exists(overview_path):
        try:
            with open(overview_path, "r", encoding="utf-8") as f:
                overview = json.load(f)
            kpi = overview.get("kpi", {})
            hi = kpi.get("high_month", 0)
            lo = kpi.get("low_month", 0)
            result["new_high_low_diff"] = (hi or 0) - (lo or 0)
            print(f"  ✅ 新高-新低: {result['new_high_low_diff']} (高{hi}/低{lo}, fallback overview)")
        except Exception as e:
            print(f"  ⚠️ 新高新低读取失败: {e}")

    # ── 3+4. 涨跌比5日均 + 5日波动率（需查DuckDB历史）──
    # v4.4: 排除当天记录，防止盘中regime写入的数据污染5日均值
    try:
        import duckdb
        if os.path.exists(DB_PATH):
            con = duckdb.connect(DB_PATH, read_only=True)

            # 检查表是否存在
            tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
            if "regime_daily" in tables:
                today_str = datetime.now().strftime("%Y-%m-%d")
                rows = con.execute("""
                    SELECT up_ratio, sh_change_pct
                    FROM regime_daily
                    WHERE up_ratio IS NOT NULL
                      AND date < ?
                    ORDER BY date DESC
                    LIMIT 5
                """, [today_str]).fetchall()

                if len(rows) >= 2:
                    # 涨跌比5日均
                    ratios = [r[0] for r in rows if r[0] is not None]
                    if ratios:
                        result["breadth_5d_avg"] = round(sum(ratios) / len(ratios), 2)
                        print(f"  ✅ 涨跌比5日均: {result['breadth_5d_avg']}% ({len(ratios)}天)")

                    # 5日波动率
                    sh_vals = [r[1] for r in rows if r[1] is not None]
                    if len(sh_vals) >= 2:
                        result["volatility_5d"] = round(statistics.stdev(sh_vals), 3)
                        print(f"  ✅ 5日波动率: {result['volatility_5d']}% ({len(sh_vals)}天)")
                else:
                    print(f"  ℹ️ 历史数据不足({len(rows)}天)，涨跌比5日均/波动率待积累")

            con.close()
    except ImportError:
        print("  ⚠️ duckdb未安装，跳过历史查询")
    except Exception as e:
        print(f"  ⚠️ 历史查询失败: {e}")

    return result


# ══════════════════════════════════════════════
# 4. Regime标签（规则引擎）
# ══════════════════════════════════════════════

def apply_regime_label(row):
    """
    规则引擎打标（按优先级）：
      trending_up    单边上涨：指数强 + 上涨比 + 追涨有效
      trending_down  单边下跌：指数弱 + 下跌比 + 抄底无效
      momentum       追涨有效：追涨显著好于抄底
      mean_reversion 抄底有效：抄底显著好于追涨
      rotating       板块轮动：高度集中 + 涨跌共存
      choppy         双向无效：以上均不满足
    """
    sh_chg  = row.get("sh_change_pct") or 0
    up_r    = row.get("up_ratio") or 0
    mom_avg = row.get("momentum_avg_return")
    rev_avg = row.get("reversion_avg_return")
    mom_up  = row.get("momentum_up_count") or 0
    rev_up  = row.get("reversion_up_count") or 0
    sec_cnt = row.get("sector_count_gainers") or 99
    overlap = row.get("sector_overlap") or 0

    has_t1 = mom_avg is not None and rev_avg is not None

    if has_t1:
        if sh_chg > 1.5 and up_r > 65 and mom_avg > 1.0:
            return "trending_up"
        if sh_chg < -1.5 and up_r < 35 and (rev_avg is None or rev_avg < 0):
            return "trending_down"
        if mom_avg > 0.5 and mom_avg > (rev_avg or 0) + 0.5:
            return "momentum"
        if (rev_avg or 0) > 0.5 and (rev_avg or 0) > mom_avg + 0.5:
            return "mean_reversion"

    if sec_cnt <= 15 and overlap >= 2:
        return "rotating"

    return "choppy"


# ══════════════════════════════════════════════
# 4b. 前置分析：今日Top100的前N日表现（v3.10）
# ══════════════════════════════════════════════

def _load_archive_spot(date_str):
    """加载指定日期的归档pkl，返回DataFrame或None"""
    fname = f"spot_{date_str.replace('-', '')}.pkl"
    path = os.path.join(ARCHIVE_SPOT_DIR, fname)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            cache = pickle.load(f)
        df = cache.get("df")
        return df if df is not None and len(df) > 0 else None
    except Exception:
        return None


def _build_close_map(df):
    """从spot df提取 code → close_price 映射"""
    col_code = next((c for c in df.columns if "代码" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)
    if not col_code or not col_price:
        return {}
    result = {}
    for _, row in df.iterrows():
        try:
            code = str(row[col_code]).replace("sz", "").replace("sh", "").replace("bj", "")
            price = float(row[col_price])
            if not math.isnan(price) and price > 0:
                result[code] = price
        except Exception:
            continue
    return result


def _extract_today_top100(df):
    """从今日spot df提取涨幅/跌幅Top100的code列表（排除新股）"""
    col_code = next((c for c in df.columns if "代码" in c), None)
    col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
    col_name = next((c for c in df.columns if "名称" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)

    if not col_code or not col_chg:
        return [], []

    valid = df.copy()
    if col_price:
        valid = valid[valid[col_price] > 0]

    valid = valid.dropna(subset=[col_chg])
    valid["_code"] = valid[col_code].astype(str).str.replace(r"^[a-z]{2}", "", regex=True)

    # 排除新股（N/C开头），排除北交所
    if col_name:
        valid = valid[~valid[col_name].astype(str).str.match(r"^[NC]")]
    valid = valid[~valid["_code"].str.startswith("8")]
    valid = valid[~valid["_code"].str.startswith("9")]

    sorted_df = valid.sort_values(col_chg, ascending=False)
    gainers = sorted_df.head(100)["_code"].tolist()
    losers = sorted_df.tail(100)["_code"].tolist()
    return gainers, losers


def _get_prior_archive_dates(today_str, n=6):
    """获取今日之前最近n个归档日期（降序），从archive目录列举"""
    if not os.path.isdir(ARCHIVE_SPOT_DIR):
        return []
    dates = []
    for f in os.listdir(ARCHIVE_SPOT_DIR):
        if f.startswith("spot_") and f.endswith(".pkl"):
            d = f[5:13]  # spot_YYYYMMDD.pkl → YYYYMMDD
            try:
                ds = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                if ds < today_str:
                    dates.append(ds)
            except Exception:
                continue
    dates.sort(reverse=True)
    return dates[:n]


def _calc_cum_returns(codes, close_map_end, close_map_start, is_gainers=True):
    """计算一组股票的累计收益率列表
    cum = (end_close / start_close - 1) * 100
    返回有效累计收益率列表
    """
    returns = []
    for code in codes:
        end_price = close_map_end.get(code)
        start_price = close_map_start.get(code)
        if end_price and start_price and start_price > 0:
            cum = (end_price / start_price - 1) * 100
            if not math.isnan(cum):
                returns.append(cum)
    return returns


def _compute_prior_metrics(returns, is_gainers=True):
    """从累计收益列表计算4个指标
    returns: [float] 累计收益率列表
    is_gainers: True=涨幅组(同向=正), False=跌幅组(同向=负)
    """
    if not returns:
        return {"same": None, "avg": None, "med": None, "strong": None}

    n = len(returns)
    if is_gainers:
        same_count = sum(1 for r in returns if r > 0)
        strong_count = sum(1 for r in returns if r > 5)
    else:
        same_count = sum(1 for r in returns if r < 0)
        strong_count = sum(1 for r in returns if r < -5)

    return {
        "same": round(same_count / n * 100, 1),
        "avg": round(statistics.mean(returns), 2),
        "med": round(statistics.median(returns), 2),
        "strong": round(strong_count / n * 100, 1),
    }


def calc_prior_analysis(df, today_date):
    """v3.10: 对当日涨跌Top100回溯前1/3/5日累计表现
    返回24个字段的dict: gn_prev{1,3,5}_{same,avg,med,strong} + ls_prev{1,3,5}_{same,avg,med,strong}
    """
    empty = {}
    for prefix in ["gn", "ls"]:
        for win in [1, 3, 5]:
            for metric in ["same", "avg", "med", "strong"]:
                empty[f"{prefix}_prev{win}_{metric}"] = None

    if df is None:
        return empty

    # 1. 提取今日Top100
    gainer_codes, loser_codes = _extract_today_top100(df)
    if not gainer_codes and not loser_codes:
        print("  ⚠️ 前置分析: 无法提取今日Top100")
        return empty

    # 2. 获取历史归档日期（最近6天够算prev5）
    prior_dates = _get_prior_archive_dates(today_date, n=6)
    if not prior_dates:
        print("  ⚠️ 前置分析: 无归档pkl，全部字段为null")
        return empty

    print(f"  ℹ️ 前置分析: 可用归档{len(prior_dates)}天 [{prior_dates[0]}..{prior_dates[-1]}]")

    result = {}

    # 3. 加载需要的归档pkl
    # prev1: T-1日涨跌幅（直接用chg_map）
    # prev3: T-1 close / T-4 close   → prior_dates[0] / prior_dates[3]
    # prev5: T-1 close / T-6 close   → prior_dates[0] / prior_dates[5]
    t1_df = _load_archive_spot(prior_dates[0]) if len(prior_dates) >= 1 else None
    t4_df = _load_archive_spot(prior_dates[3]) if len(prior_dates) >= 4 else None
    t6_df = _load_archive_spot(prior_dates[5]) if len(prior_dates) >= 6 else None

    t1_chg = build_today_chg_map(t1_df) if t1_df is not None else {}
    t1_close = _build_close_map(t1_df) if t1_df is not None else {}
    t4_close = _build_close_map(t4_df) if t4_df is not None else {}
    t6_close = _build_close_map(t6_df) if t6_df is not None else {}

    # 4. 逐窗口计算
    for prefix, codes, is_g in [("gn", gainer_codes, True), ("ls", loser_codes, False)]:
        # prev1: 直接用T-1涨跌幅
        if t1_chg:
            prev1_returns = [t1_chg[c] for c in codes if c in t1_chg]
            m = _compute_prior_metrics(prev1_returns, is_g)
        else:
            m = {"same": None, "avg": None, "med": None, "strong": None}
        for k, v in m.items():
            result[f"{prefix}_prev1_{k}"] = v

        # prev3: T-1 close / T-4 close
        if t1_close and t4_close:
            prev3_returns = _calc_cum_returns(codes, t1_close, t4_close, is_g)
            m = _compute_prior_metrics(prev3_returns, is_g)
        else:
            m = {"same": None, "avg": None, "med": None, "strong": None}
        for k, v in m.items():
            result[f"{prefix}_prev3_{k}"] = v

        # prev5: T-1 close / T-6 close
        if t1_close and t6_close:
            prev5_returns = _calc_cum_returns(codes, t1_close, t6_close, is_g)
            m = _compute_prior_metrics(prev5_returns, is_g)
        else:
            m = {"same": None, "avg": None, "med": None, "strong": None}
        for k, v in m.items():
            result[f"{prefix}_prev5_{k}"] = v

    # 填补缺失键
    for k in empty:
        if k not in result:
            result[k] = None

    filled = sum(1 for v in result.values() if v is not None)
    print(f"  ✅ 前置分析: {filled}/24字段已计算")
    return result


# ══════════════════════════════════════════════
# 5. DuckDB 建表 + 迁移 + 写入 + 导出
# ══════════════════════════════════════════════

# v3.6: 32个分档T+1字段
TIER_COLUMNS = []
for prefix in ["mom", "rev"]:
    for tier in TIERS:
        for metric in ["avg", "median", "up", "n"]:
            col_type = "DOUBLE" if metric in ("avg", "median") else "INTEGER"
            col_name = f"{prefix}_{tier}_{metric}"
            TIER_COLUMNS.append((col_name, col_type))

# v3.7+: 14个指标字段（衍生10 + 健康4）
INDICATOR_COLUMNS = [
    # 区域3：衍生指标（10个）
    ("zt_premium_avg",      "DOUBLE"),
    ("cap_scissors",        "DOUBLE"),
    ("median_change_pct",   "DOUBLE"),
    ("volume_price_ratio",  "DOUBLE"),
    ("change_pct_stdev",    "DOUBLE"),
    ("volume_concentration","DOUBLE"),
    ("extreme_ratio",       "DOUBLE"),
    ("high_price_count",    "INTEGER"),
    ("high_price_avg_chg",  "DOUBLE"),
    ("high_price_up_count", "INTEGER"),
    # 区域4：市场健康（4个）
    ("breadth_5d_avg",      "DOUBLE"),
    ("zt_dt_ratio",         "DOUBLE"),
    ("new_high_low_diff",   "INTEGER"),
    ("volatility_5d",       "DOUBLE"),
]

# 合并：迁移时需要检查的所有新列
SECTOR_DIST_COLUMNS = [
    ("sector_dist_gainers", "JSON"),   # v3.10: 板块分布（图E气泡图）
    ("sector_dist_losers",  "JSON"),   # v3.10: 板块分布（图E气泡图）
]

# v3.10: 前置分析24字段
PRIOR_COLUMNS = []
for _pf in ["gn", "ls"]:
    for _win in [1, 3, 5]:
        for _met, _typ in [("same", "DOUBLE"), ("avg", "DOUBLE"), ("med", "DOUBLE"), ("strong", "DOUBLE")]:
            PRIOR_COLUMNS.append((f"{_pf}_prev{_win}_{_met}", _typ))

ALL_NEW_COLUMNS = TIER_COLUMNS + INDICATOR_COLUMNS + SECTOR_DIST_COLUMNS + PRIOR_COLUMNS

DDL = """
CREATE TABLE IF NOT EXISTS regime_daily (
    date DATE PRIMARY KEY,

    -- 热度（复用kpi）
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

    -- 趋势：T+1次日收益（整体）
    momentum_avg_return DOUBLE,
    momentum_median_return DOUBLE,
    momentum_up_count INTEGER,
    momentum_matched INTEGER,
    reversion_avg_return DOUBLE,
    reversion_median_return DOUBLE,
    reversion_up_count INTEGER,
    reversion_matched INTEGER,

    -- v3.6：T+1次日收益（按市值四档拆分）
    mom_micro_avg DOUBLE, mom_micro_median DOUBLE, mom_micro_up INTEGER, mom_micro_n INTEGER,
    mom_small_avg DOUBLE, mom_small_median DOUBLE, mom_small_up INTEGER, mom_small_n INTEGER,
    mom_mid_avg DOUBLE,   mom_mid_median DOUBLE,   mom_mid_up INTEGER,   mom_mid_n INTEGER,
    mom_large_avg DOUBLE, mom_large_median DOUBLE, mom_large_up INTEGER, mom_large_n INTEGER,
    rev_micro_avg DOUBLE, rev_micro_median DOUBLE, rev_micro_up INTEGER, rev_micro_n INTEGER,
    rev_small_avg DOUBLE, rev_small_median DOUBLE, rev_small_up INTEGER, rev_small_n INTEGER,
    rev_mid_avg DOUBLE,   rev_mid_median DOUBLE,   rev_mid_up INTEGER,   rev_mid_n INTEGER,
    rev_large_avg DOUBLE, rev_large_median DOUBLE, rev_large_up INTEGER, rev_large_n INTEGER,

    -- 结构：市值分布
    gainer_micro INTEGER, gainer_small INTEGER,
    gainer_mid INTEGER,   gainer_large INTEGER,
    loser_micro INTEGER,  loser_small INTEGER,
    loser_mid INTEGER,    loser_large INTEGER,

    -- 结构：股价分布
    gainer_p0_10 INTEGER,  gainer_p10_30 INTEGER,
    gainer_p30_50 INTEGER, gainer_p50_100 INTEGER, gainer_p100p INTEGER,
    loser_p0_10 INTEGER,   loser_p10_30 INTEGER,
    loser_p30_50 INTEGER,  loser_p50_100 INTEGER,  loser_p100p INTEGER,

    -- 结构：板块
    sector_count_gainers INTEGER,
    sector_count_losers INTEGER,
    sector_overlap INTEGER,
    top_gainer_sectors JSON,
    top_loser_sectors JSON,
    sector_dist_gainers JSON,
    sector_dist_losers JSON,
    micro_cap_ratio_gainer DOUBLE,

    -- v3.7：衍生指标（区域3）
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

    -- v3.7：市场健康（区域4）
    breadth_5d_avg DOUBLE,
    zt_dt_ratio DOUBLE,
    new_high_low_diff INTEGER,
    volatility_5d DOUBLE,

    -- v3.10：前置分析（今日Top100的前N日表现）
    gn_prev1_same DOUBLE, gn_prev1_avg DOUBLE, gn_prev1_med DOUBLE, gn_prev1_strong DOUBLE,
    gn_prev3_same DOUBLE, gn_prev3_avg DOUBLE, gn_prev3_med DOUBLE, gn_prev3_strong DOUBLE,
    gn_prev5_same DOUBLE, gn_prev5_avg DOUBLE, gn_prev5_med DOUBLE, gn_prev5_strong DOUBLE,
    ls_prev1_same DOUBLE, ls_prev1_avg DOUBLE, ls_prev1_med DOUBLE, ls_prev1_strong DOUBLE,
    ls_prev3_same DOUBLE, ls_prev3_avg DOUBLE, ls_prev3_med DOUBLE, ls_prev3_strong DOUBLE,
    ls_prev5_same DOUBLE, ls_prev5_avg DOUBLE, ls_prev5_med DOUBLE, ls_prev5_strong DOUBLE,

    -- 标签
    regime_label VARCHAR,

    -- 元数据
    picks_date DATE,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _migrate_regime_table(con):
    """
    迁移：已有regime_daily表若缺少新列，自动ALTER TABLE添加。
    兼容 v3.6（32列）+ v3.7（14列）+ v3.10（2列板块分布JSON + 24列前置分析）
    """
    try:
        existing = set()
        for row in con.execute("PRAGMA table_info('regime_daily')").fetchall():
            existing.add(row[1])

        added = 0
        for col_name, col_type in ALL_NEW_COLUMNS:
            if col_name not in existing:
                con.execute(f"ALTER TABLE regime_daily ADD COLUMN {col_name} {col_type}")
                added += 1

        if added:
            print(f"  ✅ regime_daily迁移完成（新增{added}列）")
    except Exception as e:
        print(f"  ⚠️ regime_daily迁移失败: {e}")


def save_to_duckdb(record):
    """写入 regime_daily 表，导出最近30条到 regime_history.json"""
    try:
        import duckdb
    except ImportError:
        print("  ⚠️ duckdb未安装，跳过")
        return False

    if not os.path.exists(DB_PATH):
        print(f"  ⚠️ DuckDB不存在({DB_PATH})，跳过")
        return False

    try:
        con = duckdb.connect(DB_PATH)
        con.execute(DDL)
        _migrate_regime_table(con)

        # ── 统一构建 fields / values / placeholders 三个并行列表 ──
        json_fields = {"top_gainer_sectors", "top_loser_sectors"}

        fields = []
        values = []
        placeholders = []

        def add(name, val, is_json=False):
            fields.append(name)
            values.append(val)
            placeholders.append("json(?)" if is_json else "?")

        # 基础字段
        add("date", record["date"])
        for f in ["volume_total", "volume_rank_30d", "limit_up", "limit_down",
                   "up_count", "down_count", "up_ratio",
                   "sh_change_pct", "sz_change_pct", "cyb_change_pct", "csi1000_change_pct",
                   "momentum_avg_return", "momentum_median_return", "momentum_up_count", "momentum_matched",
                   "reversion_avg_return", "reversion_median_return", "reversion_up_count", "reversion_matched"]:
            add(f, record.get(f))

        # v3.6: 分档T+1（32个字段）
        for col_name, _ in TIER_COLUMNS:
            add(col_name, record.get(col_name))

        # 市值分布
        for f in ["gainer_micro", "gainer_small", "gainer_mid", "gainer_large",
                   "loser_micro", "loser_small", "loser_mid", "loser_large"]:
            add(f, record.get(f))

        # 股价分布
        for f in ["gainer_p0_10", "gainer_p10_30", "gainer_p30_50", "gainer_p50_100", "gainer_p100p",
                   "loser_p0_10", "loser_p10_30", "loser_p30_50", "loser_p50_100", "loser_p100p"]:
            add(f, record.get(f))

        # 板块（含JSON字段）
        for f in ["sector_count_gainers", "sector_count_losers", "sector_overlap"]:
            add(f, record.get(f))
        add("top_gainer_sectors", json.dumps(record.get("top_gainer_sectors", []), ensure_ascii=False), is_json=True)
        add("top_loser_sectors",  json.dumps(record.get("top_loser_sectors", []), ensure_ascii=False), is_json=True)
        add("sector_dist_gainers", json.dumps(record.get("sector_dist_gainers", {}), ensure_ascii=False), is_json=True)
        add("sector_dist_losers",  json.dumps(record.get("sector_dist_losers", {}), ensure_ascii=False), is_json=True)
        add("micro_cap_ratio_gainer", record.get("micro_cap_ratio_gainer"))

        # v3.7: 8个衍生/健康指标
        for col_name, _ in INDICATOR_COLUMNS:
            add(col_name, record.get(col_name))

        # v3.10: 前置分析（24个字段）
        for col_name, _ in PRIOR_COLUMNS:
            add(col_name, record.get(col_name))

        # 标签 + 元数据
        add("regime_label", record.get("regime_label"))
        add("picks_date",   record.get("picks_date"))

        # fetched_at 自动
        fields.append("fetched_at")
        placeholders.append("CURRENT_TIMESTAMP")

        sql = f"""
            INSERT OR REPLACE INTO regime_daily ({', '.join(fields)})
            VALUES ({', '.join(placeholders)})
        """

        con.execute(sql, values)

        # 导出最近30条
        rows = con.execute("""
            SELECT * FROM regime_daily
            ORDER BY date DESC LIMIT 30
        """).fetchdf()
        con.close()

        history = json.loads(rows.to_json(orient="records", date_format="iso", force_ascii=False))
        # DuckDB的JSON列以VARCHAR存储，pandas导出后是字符串，需还原为对象
        _json_fields = ("sector_dist_gainers", "sector_dist_losers",
                        "top_gainer_sectors", "top_loser_sectors")
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

        print(f"  ✅ regime_daily已写入DuckDB + 导出regime_history.json({len(history)}天)")
        return True

    except Exception as e:
        print(f"  ❌ DuckDB写入失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# ══════════════════════════════════════════════
# 6. 从 ashare_movers.json / kpi_history.json 补充热度+趋势字段
# ══════════════════════════════════════════════

def load_supplementary_data(df=None):
    """
    从已有JSON补充热度和趋势字段：
    - ashare_movers.json → 指数涨跌幅
    - kpi_history.json  → 成交额/涨停/连涨等，以及30日排名
    """
    sup = {}

    # 指数（ashare_movers.json）
    movers_path = os.path.join(DATA_DIR, "ashare_movers.json")
    if os.path.exists(movers_path):
        try:
            with open(movers_path, "r", encoding="utf-8") as f:
                movers = json.load(f)
            indices = movers.get("indices", {})
            sup["sh_change_pct"]      = indices.get("sh", {}).get("change_pct")
            sup["sz_change_pct"]      = indices.get("sz", {}).get("change_pct")
            sup["cyb_change_pct"]     = indices.get("cyb", {}).get("change_pct")
        except Exception as e:
            print(f"  ⚠️ movers.json读取失败: {e}")

    # 中证1000（ashare_overview.json）
    overview_path = os.path.join(DATA_DIR, "ashare_overview.json")
    if os.path.exists(overview_path):
        try:
            with open(overview_path, "r", encoding="utf-8") as f:
                overview = json.load(f)
            cap_idx = overview.get("cap_indices", {})
            sup["csi1000_change_pct"] = cap_idx.get("small", {}).get("change_pct")

            # KPI热度 — v4.5: fetched_at日期校验，防race condition（ashare_overview.json可能是昨日旧值）
            kpi = overview.get("kpi", {})
            sup["volume_total"] = kpi.get("volume_total")

            overview_date = (overview.get("fetched_at") or "")[:10]
            today_str = datetime.now().strftime("%Y-%m-%d")
            kpi_fresh = (overview_date == today_str)

            if kpi_fresh:
                sup["limit_up"]   = kpi.get("limit_up")
                sup["limit_down"] = kpi.get("limit_down")
                sup["up_count"]   = kpi.get("up_count")
                sup["down_count"] = kpi.get("down_count")
                total = (kpi.get("up_count") or 0) + (kpi.get("down_count") or 0) + (kpi.get("flat_count") or 0)
                sup["up_ratio"] = round((kpi.get("up_count") or 0) / total * 100, 1) if total else None
                print(f"  ✅ KPI热度: 来自overview.json ({overview_date})")
            else:
                print(f"  ⚠️ overview.json日期={overview_date}，非今日，KPI热度从spot df现场计算")
                if df is not None:
                    col_chg  = next((c for c in df.columns if "涨跌幅" in c), None)
                    col_code = next((c for c in df.columns if "代码" in c), None)
                    if col_chg and col_code:
                        # 排除北交所
                        mask_bj = df[col_code].astype(str).str.startswith("bj")
                        df_a = df[~mask_bj]
                        chg = df_a[col_chg].apply(lambda x: float(x) if x not in (None, "", "—") else float("nan"))
                        sup["up_count"]   = int((chg > 0).sum())
                        sup["down_count"] = int((chg < 0).sum())
                        flat = int((chg == 0).sum())
                        total = sup["up_count"] + sup["down_count"] + flat
                        sup["up_ratio"] = round(sup["up_count"] / total * 100, 1) if total else None
                    lu, ld = calc_limit_counts(df)
                    sup["limit_up"]   = lu
                    sup["limit_down"] = ld
                    print(f"  ✅ KPI热度: spot df现场计算 up={sup['up_count']} down={sup['down_count']} lu={lu} ld={ld}")
                else:
                    print(f"  ⚠️ df不可用，KPI热度字段留null")
        except Exception as e:
            print(f"  ⚠️ overview.json读取失败: {e}")

    # v4.3: 指数null时从derived_intraday.json补（腾讯fallback源）
    idx_keys = ["sh_change_pct", "sz_change_pct", "cyb_change_pct", "csi1000_change_pct"]
    if any(sup.get(k) is None for k in idx_keys):
        derived_path = os.path.join(DATA_DIR, "derived_intraday.json")
        if os.path.exists(derived_path):
            try:
                with open(derived_path, "r", encoding="utf-8") as f:
                    derived = json.load(f)
                # snapshots数组结构，取最后一个（最新时间点）
                snapshots = derived.get("snapshots", [])
                if snapshots:
                    latest = snapshots[-1]
                    for k in idx_keys:
                        if sup.get(k) is None and latest.get(k) is not None:
                            sup[k] = latest[k]
                    filled = sum(1 for k in idx_keys if sup.get(k) is not None)
                    print(f"  ✅ 指数从derived_intraday.json补充 ({filled}/4)")
            except Exception as e:
                print(f"  ⚠️ derived_intraday.json读取失败: {e}")

    # 30日排名（kpi_history.json）
    hist_path = os.path.join(DATA_DIR, "kpi_history.json")
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                hist = json.load(f)
            if hist and sup.get("volume_total") is not None:
                vols = [h.get("volume_total", 0) for h in hist if h.get("volume_total")]
                today_vol = sup["volume_total"]
                rank = sum(1 for v in vols if v < today_vol)
                sup["volume_rank_30d"] = rank
        except Exception as e:
            print(f"  ⚠️ kpi_history.json读取失败: {e}")

    return sup


# ══════════════════════════════════════════════
# 7. 主函数
# ══════════════════════════════════════════════

def collect_regime():
    """主入口"""
    print("[regime_collector] 开始计算...")

    # 1. 加载picks
    picks = load_yesterday_picks()
    if picks is None:
        print("[regime_collector] 无picks数据，退出")
        return None

    gainers = picks.get("top100_gainers", [])
    losers  = picks.get("top100_losers", [])
    picks_date = picks.get("date")

    # 2. 加载今日全市场
    df = load_spot_cache()

    # 3. T+1次日收益
    today_date = datetime.now().strftime("%Y-%m-%d")
    mom_returns = {"avg": None, "median": None, "up_count": 0, "matched": 0}
    rev_returns = {"avg": None, "median": None, "up_count": 0, "matched": 0}
    mom_tier = {t: {"avg": None, "median": None, "up_count": 0, "n": 0} for t in TIERS}
    rev_tier = {t: {"avg": None, "median": None, "up_count": 0, "n": 0} for t in TIERS}

    if df is not None and picks_date != today_date:
        chg_map = build_today_chg_map(df)
        mom_returns = calc_next_day_returns(gainers, chg_map)
        rev_returns = calc_next_day_returns(losers, chg_map)
        print(f"  ✅ 追涨次日: avg={mom_returns['avg']}%, 上涨{mom_returns['up_count']}/{mom_returns['matched']}")
        print(f"  ✅ 抄底次日: avg={rev_returns['avg']}%, 上涨{rev_returns['up_count']}/{rev_returns['matched']}")

        mom_tier = calc_next_day_returns_by_tier(gainers, chg_map)
        rev_tier = calc_next_day_returns_by_tier(losers, chg_map)
        for t in TIERS:
            mt = mom_tier[t]
            rt = rev_tier[t]
            print(f"    {t}: 追涨avg={mt['avg']}%({mt['n']}只) 抄底avg={rt['avg']}%({rt['n']}只)")

    elif picks_date == today_date:
        print(f"  ℹ️ picks是今日数据（{picks_date}），T+1收益待明日计算")
    else:
        print(f"  ⚠️ 无行情缓存，T+1收益跳过")

    # 4. 当日分布
    gainer_cap   = calc_cap_dist(gainers)
    loser_cap    = calc_cap_dist(losers)
    gainer_price = calc_price_dist(gainers)
    loser_price  = calc_price_dist(losers)
    sector_stats = calc_sector_stats(gainers, losers)

    print(f"  ✅ 涨幅Top100: 微{gainer_cap['微盘']}/小{gainer_cap['小盘']}/中{gainer_cap['中盘']}/大{gainer_cap['大盘']}")
    print(f"  ✅ 板块集中度: 涨{sector_stats['sector_count_gainers']}板块, 重合{sector_stats['sector_overlap']}个")
    print(f"  ✅ 微盘热度: {sector_stats['micro_cap_ratio_gainer']}%")

    # 5. 补充热度+趋势字段
    sup = load_supplementary_data(df=df)

    # v3.8: 自算涨停/跌停（不依赖ashare_overview.json，消除执行顺序依赖）
    if df is not None:
        lu, ld = calc_limit_counts(df)
        sup["limit_up"] = lu
        sup["limit_down"] = ld
        # 同时补算 up_count/down_count/up_ratio（如果overview没跑完）
        col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
        col_price = next((c for c in df.columns if "最新价" in c), None)
        if col_chg and sup.get("up_count") is None:
            valid = df[df[col_price] > 0] if col_price else df
            sup["up_count"] = int((valid[col_chg] > 0).sum())
            sup["down_count"] = int((valid[col_chg] < 0).sum())
            total = len(valid)
            sup["up_ratio"] = round(sup["up_count"] / total * 100, 1) if total else None

    # 6. v3.7新增：衍生指标 + 市场健康
    derived = calc_derived_indicators(df, gainers, losers, sup)
    health  = calc_health_indicators(sup)

    # 6b. v3.10新增：前置分析（今日Top100的前N日表现）
    prior = calc_prior_analysis(df, today_date)

    # 7. 组装record
    record = {
        "date": today_date,
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

        # v3.7: 衍生指标
        **derived,
        # v3.7: 市场健康
        **health,
        # v3.10: 前置分析
        **prior,
    }

    # v3.6: 分档T+1写入record
    for prefix, tier_data in [("mom", mom_tier), ("rev", rev_tier)]:
        for tier in TIERS:
            td = tier_data[tier]
            record[f"{prefix}_{tier}_avg"]    = td["avg"]
            record[f"{prefix}_{tier}_median"] = td["median"]
            record[f"{prefix}_{tier}_up"]     = td["up_count"]
            record[f"{prefix}_{tier}_n"]      = td["n"]

    # 8. 打regime标签
    record["regime_label"] = apply_regime_label(record)
    print(f"  ✅ regime_label: {record['regime_label']}")

    # 9. 写DuckDB + 导出JSON
    save_to_duckdb(record)

    return record


if __name__ == "__main__":
    collect_regime()
