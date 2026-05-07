"""
慧盘 · 前置分析（纯计算）
v5.1 — 在 v5.0 基础上加分布直方图 (hist) 字段

v5.0: 对当日涨跌 Top100 回溯前 1/3/5 日累计表现，产出 24 个字段
v5.1: 每个窗口额外输出 9 桶分布直方图, 共 30 个字段

新增字段:
  gn_prev1_hist, gn_prev3_hist, gn_prev5_hist
  ls_prev1_hist, ls_prev3_hist, ls_prev5_hist
  每个是长度 9 的 list, 桶边界见 BUCKET_EDGES

规则：本模块不读写文件。归档 pkl 的加载由 IO 层完成后传入。
"""

import math
import statistics


# ─────────────────────────────────────────────────────────
# v5.1: 分布直方图分桶边界
# 9 个桶, 涨/跌两组共用同一组边界 (视觉对称, 便于对照)
#   <=-10%   -10~-5%   -5~-2%   -2~0%   0~2%   2~5%   5~10%   10~15%   >15%
# 桶数=9, 边界数=8 (左闭右开, 末桶左闭右无界)
# ─────────────────────────────────────────────────────────
BUCKET_EDGES = [-10.0, -5.0, -2.0, 0.0, 2.0, 5.0, 10.0, 15.0]
BUCKET_LABELS = ["<=-10%", "-10~-5%", "-5~-2%", "-2~0%", "0~2%", "2~5%", "5~10%", "10~15%", ">15%"]


def _compute_histogram(returns):
    """对累计收益列表分桶, 返回长度 9 的计数 list

    桶定义 (左闭右开):
      bucket[0] = (-inf, -10)
      bucket[1] = [-10, -5)
      bucket[2] = [-5, -2)
      bucket[3] = [-2, 0)
      bucket[4] = [0, 2)
      bucket[5] = [2, 5)
      bucket[6] = [5, 10)
      bucket[7] = [10, 15)
      bucket[8] = [15, +inf)
    """
    if not returns:
        return None
    buckets = [0] * 9
    for r in returns:
        if r is None or (isinstance(r, float) and math.isnan(r)):
            continue
        # 找到 r 应该落入的桶 idx
        idx = 0
        for edge in BUCKET_EDGES:
            if r < edge:
                break
            idx += 1
        buckets[idx] += 1
    return buckets


def build_close_map(df):
    """从 spot df 提取 code → close_price 映射"""
    col_code  = next((c for c in df.columns if "代码" in c), None)
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


def extract_today_top100(df):
    """从今日 spot df 提取涨幅/跌幅 Top100 的 code 列表（排除新股+北交所）"""
    col_code  = next((c for c in df.columns if "代码" in c), None)
    col_chg   = next((c for c in df.columns if "涨跌幅" in c), None)
    col_name  = next((c for c in df.columns if "名称" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)

    if not col_code or not col_chg:
        return [], []

    valid = df.copy()
    if col_price:
        valid = valid[valid[col_price] > 0]

    valid = valid.dropna(subset=[col_chg])
    valid["_code"] = valid[col_code].astype(str).str.replace(r"^[a-z]{2}", "", regex=True)

    # 排除新股（N/C 开头），排除北交所
    if col_name:
        valid = valid[~valid[col_name].astype(str).str.match(r"^[NC]")]
    valid = valid[~valid["_code"].str.startswith("8")]
    valid = valid[~valid["_code"].str.startswith("9")]

    sorted_df = valid.sort_values(col_chg, ascending=False)
    gainers = sorted_df.head(100)["_code"].tolist()
    losers  = sorted_df.tail(100)["_code"].tolist()
    return gainers, losers


def _calc_cum_returns(codes, close_map_end, close_map_start):
    """计算一组股票的累计收益率列表"""
    returns = []
    for code in codes:
        end_price   = close_map_end.get(code)
        start_price = close_map_start.get(code)
        if end_price and start_price and start_price > 0:
            cum = (end_price / start_price - 1) * 100
            if not math.isnan(cum):
                returns.append(cum)
    return returns


def _compute_prior_metrics(returns, is_gainers=True):
    """从累计收益列表计算 5 个指标 (v5.1: 加 hist)"""
    if not returns:
        return {"same": None, "avg": None, "med": None, "strong": None, "hist": None}

    n = len(returns)
    if is_gainers:
        same_count   = sum(1 for r in returns if r > 0)
        strong_count = sum(1 for r in returns if r > 5)
    else:
        same_count   = sum(1 for r in returns if r < 0)
        strong_count = sum(1 for r in returns if r < -5)

    return {
        "same":   round(same_count / n * 100, 1),
        "avg":    round(statistics.mean(returns), 2),
        "med":    round(statistics.median(returns), 2),
        "strong": round(strong_count / n * 100, 1),
        "hist":   _compute_histogram(returns),
    }


def calc_prior_analysis(df, chg_map_t1, close_map_t1, close_map_t4, close_map_t6):
    """
    纯计算：对当日涨跌 Top100 回溯前 1/3/5 日累计表现。

    参数:
      df            — 今日全市场 DataFrame
      chg_map_t1    — T-1 日涨跌幅映射 {code: chg_pct}
      close_map_t1  — T-1 日收盘价映射 {code: price}
      close_map_t4  — T-4 日收盘价映射（用于 prev3）
      close_map_t6  — T-6 日收盘价映射（用于 prev5）

    返回 dict, 30 个字段:
      gn_prev{1,3,5}_{same,avg,med,strong,hist} (15)
      ls_prev{1,3,5}_{same,avg,med,strong,hist} (15)
      其中 hist 字段为长度 9 的 list (None 如无数据)
    """
    # 初始化空结果
    empty = {}
    for prefix in ["gn", "ls"]:
        for win in [1, 3, 5]:
            for metric in ["same", "avg", "med", "strong", "hist"]:
                empty[f"{prefix}_prev{win}_{metric}"] = None

    if df is None:
        return empty

    # 1. 提取今日 Top100
    gainer_codes, loser_codes = extract_today_top100(df)
    if not gainer_codes and not loser_codes:
        print("  ⚠️ 前置分析: 无法提取今日Top100")
        return empty

    result = {}

    # 2. 逐窗口计算
    for prefix, codes, is_g in [("gn", gainer_codes, True), ("ls", loser_codes, False)]:
        # prev1: 直接用 T-1 涨跌幅
        if chg_map_t1:
            prev1_returns = [chg_map_t1[c] for c in codes if c in chg_map_t1]
            m = _compute_prior_metrics(prev1_returns, is_g)
        else:
            m = {"same": None, "avg": None, "med": None, "strong": None, "hist": None}
        for k, v in m.items():
            result[f"{prefix}_prev1_{k}"] = v

        # prev3: T-1 close / T-4 close
        if close_map_t1 and close_map_t4:
            prev3_returns = _calc_cum_returns(codes, close_map_t1, close_map_t4)
            m = _compute_prior_metrics(prev3_returns, is_g)
        else:
            m = {"same": None, "avg": None, "med": None, "strong": None, "hist": None}
        for k, v in m.items():
            result[f"{prefix}_prev3_{k}"] = v

        # prev5: T-1 close / T-6 close
        if close_map_t1 and close_map_t6:
            prev5_returns = _calc_cum_returns(codes, close_map_t1, close_map_t6)
            m = _compute_prior_metrics(prev5_returns, is_g)
        else:
            m = {"same": None, "avg": None, "med": None, "strong": None, "hist": None}
        for k, v in m.items():
            result[f"{prefix}_prev5_{k}"] = v

    # 填补缺失键
    for k in empty:
        if k not in result:
            result[k] = None

    filled = sum(1 for v in result.values() if v is not None)
    print(f"  ✅ 前置分析: {filled}/30字段已计算 (含 hist)")
    return result
