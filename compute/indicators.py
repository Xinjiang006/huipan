"""
慧盘 · 纯计算函数（指标/分布/T+1/标签）
v5.0 — 从 regime_collector.py 剥离

规则：本模块不 import json/os/pickle/duckdb，不读写任何文件。
     所有数据通过参数传入，结果通过 return 传出。
"""

import math
import statistics

# 市值档位常量
TIERS = ["micro", "small", "mid", "large"]
TIER_LABEL_MAP = {"微盘": "micro", "小盘": "small", "中盘": "mid", "大盘": "large"}


# ══════════════════════════════════════════════
# 1. 辅助：列名探测 + DataFrame 清洗
# ══════════════════════════════════════════════

def _col(df, keyword):
    """从 DataFrame 列名中找包含 keyword 的第一个列名"""
    return next((c for c in df.columns if keyword in c), None)


def _clean_df(df):
    """清洗全市场 df：排除北交所、价格<=0、涨跌幅为空。返回 (valid_df, col_dict)"""
    cols = {
        "code":  _col(df, "代码"),
        "name":  _col(df, "名称"),
        "chg":   _col(df, "涨跌幅"),
        "open":  _col(df, "今开"),
        "close": _col(df, "昨收"),
        "vol":   _col(df, "成交额"),
        "price": _col(df, "最新价"),
    }

    valid = df.copy()
    if cols["price"]:
        valid = valid[valid[cols["price"]] > 0]
    if cols["chg"]:
        valid = valid[valid[cols["chg"]].notna()]
    if cols["code"]:
        valid = valid[~valid[cols["code"]].str.startswith("bj")]
        valid["_code"] = valid[cols["code"]].str.replace(r"^[a-z]{2}", "", regex=True)

    return valid, cols


# ══════════════════════════════════════════════
# 2. T+1 次日收益
# ══════════════════════════════════════════════

def build_today_chg_map(df):
    """从全市场 df 提取 code → today_change_pct 映射"""
    col_code  = _col(df, "代码")
    col_chg   = _col(df, "涨跌幅")
    col_price = _col(df, "最新价")

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


def calc_next_day_returns(picks_list, chg_map):
    """
    计算 picks 列表中股票在 T+1 日的收益统计。
    排除 is_new=True 的新股。
    返回 dict: avg, median, up_count, matched
    """
    returns = []
    for stock in picks_list:
        if stock.get("is_new"):
            continue
        chg = chg_map.get(stock.get("code", ""))
        if chg is not None:
            returns.append(chg)

    if not returns:
        return {"avg": None, "median": None, "up_count": 0, "matched": 0}

    return {
        "avg": round(sum(returns) / len(returns), 3),
        "median": round(statistics.median(returns), 3),
        "up_count": sum(1 for r in returns if r > 0),
        "matched": len(returns),
    }


def calc_next_day_returns_by_tier(picks_list, chg_map):
    """
    按市值四档分别计算 T+1 次日收益。
    返回 dict: { "micro": {avg, median, up_count, n}, ... }
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
        returns = []
        for s in tier_stocks[tier]:
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
# 3. 当日分布
# ══════════════════════════════════════════════

def calc_cap_dist(picks_list):
    """市值档位分布：微/小/中/大各多少只"""
    dist = {"微盘": 0, "小盘": 0, "中盘": 0, "大盘": 0, "未知": 0}
    for s in picks_list:
        label = s.get("cap_label", "未知")
        dist[label] = dist.get(label, 0) + 1
    return dist


def calc_price_dist(picks_list):
    """股价档位分布：0-10/10-30/30-50/50-100/100+ 各多少只"""
    dist = {"0-10": 0, "10-30": 0, "30-50": 0, "50-100": 0, "100+": 0, "未知": 0}
    for s in picks_list:
        label = s.get("price_label", "未知")
        dist[label] = dist.get(label, 0) + 1
    return dist


def calc_sector_stats(gainers, losers):
    """板块统计：涨跌分布 + 集中度 + 重合度"""
    from collections import Counter

    def count_sectors(picks_list):
        sectors = [s.get("sector", "未知") for s in picks_list if s.get("sector") != "未知"]
        return Counter(sectors)

    gainer_sectors = count_sectors(gainers)
    loser_sectors  = count_sectors(losers)

    gainer_top5 = set(s for s, _ in gainer_sectors.most_common(5))
    loser_top5  = set(s for s, _ in loser_sectors.most_common(5))

    micro_count = sum(1 for s in gainers if s.get("cap_label") == "微盘")
    micro_ratio = round(micro_count / len(gainers) * 100, 1) if gainers else 0

    return {
        "sector_count_gainers": len(gainer_sectors),
        "sector_count_losers":  len(loser_sectors),
        "sector_overlap":       len(gainer_top5 & loser_top5),
        "top_gainer_sectors":   [s for s, cnt in gainer_sectors.most_common() if cnt >= 5],
        "top_loser_sectors":    [s for s, cnt in loser_sectors.most_common() if cnt >= 5],
        "sector_dist_gainers":  dict(gainer_sectors),
        "sector_dist_losers":   dict(loser_sectors),
        "concentration_hhi":     round(sum((c/len(gainers)*100)**2 for c in gainer_sectors.values())) if gainers else None,
        "micro_cap_ratio_gainer": micro_ratio,
        # HHI板块集中度（Top100赫芬达尔指数）
        "concentration_hhi": round(sum((c / max(sum(gainer_sectors.values()), 1) * 100) ** 2 for c in gainer_sectors.values())),
    }


# ══════════════════════════════════════════════
# 4. 衍生指标（区域3：10个）
# ══════════════════════════════════════════════

def calc_derived_indicators(df, gainers, losers, sup, yd_limit_up_codes=None):
    """
    纯计算：衍生指标 10 个。

    参数:
      df               — 全市场 DataFrame
      gainers, losers  — 今日 Top100 picks 列表
      sup              — 补充数据 dict（含指数涨跌幅等）
      yd_limit_up_codes — 昨日涨停股代码 set（由 IO 层提供）

    返回 dict, 10 个字段
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

    valid, cols = _clean_df(df)

    # ── 1. 涨停溢价率 ──
    if cols["open"] and cols["close"] and yd_limit_up_codes:
        premiums = []
        for _, row in valid.iterrows():
            if row.get("_code") in yd_limit_up_codes:
                try:
                    open_p = float(row[cols["open"]])
                    prev_c = float(row[cols["close"]])
                    if prev_c > 0 and open_p > 0:
                        premiums.append((open_p - prev_c) / prev_c * 100)
                except (ValueError, TypeError):
                    continue
        if premiums:
            result["zt_premium_avg"] = round(sum(premiums) / len(premiums), 3)
            print(f"  ✅ 溢价率: {result['zt_premium_avg']}% ({len(premiums)}只涨停匹配)")
        elif yd_limit_up_codes:
            print(f"  ⚠️ 溢价率: 昨日{len(yd_limit_up_codes)}只涨停，今日无匹配")

    # ── 2. 大小盘剪刀差 ──
    sh = sup.get("sh_change_pct")
    csi = sup.get("csi1000_change_pct")
    if sh is not None and csi is not None:
        result["cap_scissors"] = round(sh - csi, 3)
        print(f"  ✅ 剪刀差: {result['cap_scissors']}% (沪深300{sh}% - 中证1000{csi}%)")

    # ── 3. 全市场涨跌幅中位数 ──
    if cols["chg"]:
        try:
            chg_vals = valid[cols["chg"]].dropna().astype(float).tolist()
            chg_vals = [v for v in chg_vals if not math.isnan(v)]
            if chg_vals:
                result["median_change_pct"] = round(statistics.median(chg_vals), 3)
                print(f"  ✅ 中位数涨幅: {result['median_change_pct']}% ({len(chg_vals)}只)")
        except Exception as e:
            print(f"  ⚠️ 中位数计算失败: {e}")

    # ── 4. 量价配合度 ──
    if cols["vol"] and cols["chg"]:
        try:
            sorted_df = valid.sort_values(cols["chg"], ascending=False)
            top100_up = sorted_df.head(100)
            top100_dn = sorted_df.tail(100)
            avg_vol_up = top100_up[cols["vol"]].astype(float).mean()
            avg_vol_dn = top100_dn[cols["vol"]].astype(float).mean()
            if avg_vol_dn > 0:
                result["volume_price_ratio"] = round(avg_vol_up / avg_vol_dn, 3)
                print(f"  ✅ 量价配合: {result['volume_price_ratio']}x")
        except Exception as e:
            print(f"  ⚠️ 量价配合计算失败: {e}")

    # ── 5. 涨跌幅标准差 ──
    if cols["chg"]:
        try:
            chg_vals = valid[cols["chg"]].dropna().astype(float).tolist()
            chg_vals = [v for v in chg_vals if not math.isnan(v)]
            if len(chg_vals) >= 2:
                result["change_pct_stdev"] = round(statistics.stdev(chg_vals), 3)
                print(f"  ✅ 涨跌幅标准差: {result['change_pct_stdev']}%")
        except Exception as e:
            print(f"  ⚠️ 标准差计算失败: {e}")

    # ── 6. 成交额集中度（Top10 占比）──
    if cols["vol"]:
        try:
            vol_vals = valid[cols["vol"]].dropna().astype(float)
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
    if cols["chg"]:
        try:
            chg_series = valid[cols["chg"]].dropna().astype(float)
            up5 = int((chg_series > 5).sum())
            dn5 = int((chg_series < -5).sum())
            result["extreme_ratio"] = round(up5 / max(dn5, 1), 2)
            print(f"  ✅ 极端涨跌比: {result['extreme_ratio']}x ({up5}涨>{dn5}跌)")
        except Exception as e:
            print(f"  ⚠️ 极端比计算失败: {e}")

    # ── 8/9/10. 高价股（>100 元）──
    if cols["price"] and cols["chg"]:
        try:
            hp = valid[valid[cols["price"]].astype(float) > 100].copy()
            hp_count = len(hp)
            result["high_price_count"] = hp_count
            if hp_count > 0:
                hp_chg = hp[cols["chg"]].astype(float)
                result["high_price_avg_chg"] = round(hp_chg.mean(), 3)
                result["high_price_up_count"] = int((hp_chg > 0).sum())
                print(f"  ✅ 高价股: {hp_count}只, avg={result['high_price_avg_chg']}%, 上涨{result['high_price_up_count']}")
            else:
                result["high_price_avg_chg"] = None
                result["high_price_up_count"] = 0
                print("  ℹ️ 高价股: 0只")
        except Exception as e:
            print(f"  ⚠️ 高价股计算失败: {e}")

    return result


def calc_intraday_strength(df):
    """日内强度中位数：(收盘-最低)/(最高-最低) × 100
    100% = 收在最高点（强势），0% = 收在最低点（冲高回落）
    """
    valid, cols = _clean_df(df)
    price_col = cols["price"]
    if price_col is None:
        return None

    high_col = next((c for c in df.columns if "最高" in c), None)
    low_col = next((c for c in df.columns if "最低" in c), None)
    if high_col is None or low_col is None:
        return None

    try:
        high = valid[high_col].astype(float)
        low = valid[low_col].astype(float)
        close = valid[price_col].astype(float)

        amplitude = high - low
        mask = amplitude > 0  # 排除一字板/停牌
        if mask.sum() == 0:
            return None

        strength = (close[mask] - low[mask]) / amplitude[mask] * 100
        result = round(float(strength.median()), 2)
        print(f"  ✅ 日内强度: {result}% ({mask.sum()}只参与)")
        return result
    except Exception as e:
        print(f"  ⚠️ 日内强度计算失败: {e}")
        return None


def calc_vwap_bias(df):
    """VWAP偏离中位数：(收盘-均价)/均价 × 100
    正值 = 尾盘资金净买入，负值 = 冲高出货
    均价 = 成交额 / 成交量
    """
    valid, cols = _clean_df(df)
    price_col = cols["price"]
    vol_col = cols["vol"]  # 成交额
    if price_col is None or vol_col is None:
        return None

    volume_col = next((c for c in df.columns if "成交量" in c), None)
    if volume_col is None:
        return None

    try:
        amount = valid[vol_col].astype(float)
        volume = valid[volume_col].astype(float)
        close = valid[price_col].astype(float)

        # 排除无成交
        mask = (volume > 0) & (amount > 0)
        if mask.sum() == 0:
            return None

        vwap = amount[mask] / volume[mask]
        # 排除vwap异常
        vwap_mask = vwap > 0
        if vwap_mask.sum() == 0:
            return None

        bias = (close[mask][vwap_mask] - vwap[vwap_mask]) / vwap[vwap_mask] * 100
        result = round(float(bias.median()), 2)
        print(f"  ✅ VWAP偏离: {result}% ({vwap_mask.sum()}只参与)")
        return result
    except Exception as e:
        print(f"  ⚠️ VWAP偏离计算失败: {e}")
        return None


# ══════════════════════════════════════════════
# 5. 市场健康指标（区域4：4个）
# ══════════════════════════════════════════════

def calc_health_indicators(df, sup, history_data=None):
    """
    纯计算：健康指标 4 个。

    参数:
      df               — 全市场 DataFrame（用于计算日内强度和VWAP偏离）
      sup              — 补充数据 dict
      history_data     — DuckDB 查询结果 dict:
                         {"up_ratios": [float], "sh_changes": [float]}
                         由 IO 层查询后传入

    返回 dict, 4 个字段
    """
    result = {
        "breadth_5d_avg": None,
        "vwap_bias_median": None,
        "intraday_strength_median": None,
        "volatility_5d": None,
    }

    # ── 1. VWAP偏离中位数（替换涨停/跌停比）──
    if df is not None:
        result["vwap_bias_median"] = calc_vwap_bias(df)

    # ── 2. 日内强度中位数（替换新高-新低差）──
    if df is not None:
        result["intraday_strength_median"] = calc_intraday_strength(df)

    # ── 3+4. 涨跌比5日均 + 5日波动率 ──
    if history_data:
        ratios = history_data.get("up_ratios", [])
        if ratios:
            result["breadth_5d_avg"] = round(sum(ratios) / len(ratios), 2)
            print(f"  ✅ 涨跌比5日均: {result['breadth_5d_avg']}% ({len(ratios)}天)")

        sh_vals = history_data.get("sh_changes", [])
        if len(sh_vals) >= 2:
            result["volatility_5d"] = round(statistics.stdev(sh_vals), 3)
            print(f"  ✅ 5日波动率: {result['volatility_5d']}%")

    return result


# ══════════════════════════════════════════════
# 6. Regime 标签规则引擎
# ══════════════════════════════════════════════

def apply_regime_label(row):
    """
    规则引擎打标（按优先级）：
      trending_up    单边上涨
      trending_down  单边下跌
      momentum       追涨有效
      mean_reversion 抄底有效
      rotating       板块轮动
      choppy         双向无效
    """
    sh_chg  = row.get("sh_change_pct") or 0
    up_r    = row.get("up_ratio") or 0
    mom_avg = row.get("momentum_avg_return")
    rev_avg = row.get("reversion_avg_return")
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
# 以下内容追加到 compute/indicators.py 末尾
# v5.7 新增 · BIAS（乖离率）通用技术指标
# ══════════════════════════════════════════════


def calc_bias(current_price, historical_prices, n):
    """计算 N 日 BIAS（乖离率）

    BIAS_N = (current_price - avg(最近 N 日收盘价)) / avg × 100

    参数：
        current_price: 当前价
        historical_prices: 历史收盘价序列（降序，[T-1, T-2, ...]）
                          停牌日应在调用方过滤掉，不应在此补齐
        n: BIAS 周期（典型 5 或 10）

    返回：BIAS（正值升水，负值折价），或 None（数据不足/异常）

    异常处理：
        - 样本量太少（< n-2）→ None
        - current_price <= 0 → None
        - avg <= 0 → None
        - 其他异常（TypeError 等）→ None

    注：调用方应保证 historical_prices 是非停牌日的有效价格。
       长期停牌股应在候选筛选阶段（#0 前置过滤）已被剔除。
    """
    try:
        if current_price is None or current_price <= 0:
            return None
        if not historical_prices:
            return None
        # 允许最多 2 天缺失（停牌 1-2 天仍能计算）
        if len(historical_prices) < n - 2:
            return None

        window = historical_prices[:n]
        if not window:
            return None

        avg = sum(window) / len(window)
        if avg <= 0:
            return None

        return round((current_price - avg) / avg * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def calc_bias_pair(current_price, historical_prices):
    """同时计算 BIAS_5 和 BIAS_10，返回更深的作为 bias_min

    参数：
        current_price: 当前价
        historical_prices: 历史收盘价序列（降序）

    返回：
        {
            "bias_5": float | None,
            "bias_10": float | None,
            "bias_min": float | None,   # 两者中更小的（更折价的）
            "reason": str | None,        # None=正常; "insufficient_history" / "invalid_price" 等
        }

    用例（止跌反转加权）：
        - bias_min > -3: 没跌透，乘数 0.0（清零）
        - -3 ~ -5: 轻度回调，乘数 0.5
        - -5 ~ -8: 中度回调，乘数 1.0
        - -8 ~ -12: 深度回调，乘数 1.2
        - < -12: 超跌，乘数 1.3
        - None: 数据异常，乘数 0.8（轻度降权，不清零）
    """
    # 预检
    if current_price is None or current_price <= 0:
        return {"bias_5": None, "bias_10": None, "bias_min": None, "reason": "invalid_price"}
    if not historical_prices:
        return {"bias_5": None, "bias_10": None, "bias_min": None, "reason": "no_history"}
    if len(historical_prices) < 3:
        return {
            "bias_5": None, "bias_10": None, "bias_min": None,
            "reason": f"insufficient_history({len(historical_prices)}<3)",
        }

    bias_5 = calc_bias(current_price, historical_prices, 5)
    bias_10 = calc_bias(current_price, historical_prices, 10)

    candidates = [b for b in (bias_5, bias_10) if b is not None]
    if not candidates:
        return {
            "bias_5": bias_5, "bias_10": bias_10, "bias_min": None,
            "reason": "both_bias_failed",
        }

    bias_min = min(candidates)
    return {
        "bias_5": bias_5,
        "bias_10": bias_10,
        "bias_min": bias_min,
        "reason": None,
    }
