"""
慧盘 · 股票过滤与分类工具
v5.1 — 涨停判定改为价格判定（最新价 == 涨停价）

设计原则：
  - 纯函数，不碰文件系统
  - 输入DataFrame/Series，输出DataFrame/bool/int
  - 单一实现：涨停判定、北交所过滤、有效股过滤只在这里写一遍

口径说明：
  - 默认排除北交所（BJ）：流动性差、±30%涨跌幅、与主板交易者群体不同
  - 涨停阈值区分板制：主板10% / 创业板科创板20% / ST 5% / 北交所30%
  - 有效股 = 最新价>0 且 涨跌幅非空（排除停牌、未开盘）

v5.1 涨停判定变更：
  - 旧：涨跌幅 >= 阈值 - 0.2%（容差法，会把未封板的算进去）
  - 新：最新价 >= 涨停价（涨停价 = 四舍五入(昨收 × (1+阈值%), 2位)）
  - ST不计入涨停/跌停统计（与同花顺一致）
  - 新股(N/C)不计入涨停/跌停统计（新股首日涨跌幅无参考意义）
"""

import math
import re

# ══════════════════════════════════════════════
# 1. 代码清洗
# ══════════════════════════════════════════════

def strip_prefix(code):
    """sh600000 → 600000, sz300001 → 300001, bj430001 → 430001"""
    return re.sub(r"^[a-z]{2}", "", str(code))


def get_exchange(code):
    """
    返回交易所前缀: 'sh' / 'sz' / 'bj' / ''
    支持带前缀(sh600000)和纯数字(600000)两种输入
    """
    code = str(code)
    if code.startswith(("sh", "sz", "bj")):
        return code[:2]

    c = strip_prefix(code)
    if c.startswith("6"):
        return "sh"
    elif c.startswith(("0", "3")):
        return "sz"
    elif c.startswith(("4", "8")):
        return "bj"
    return ""


def is_bj(code):
    """是否北交所股票"""
    code = str(code)
    if code.startswith("bj"):
        return True
    c = strip_prefix(code)
    return c.startswith(("4", "8")) and len(c) == 6


def is_gem(code):
    """是否创业板(Growth Enterprise Market, sz30xxxx)"""
    code = str(code)
    return code.startswith("sz3") or (not code.startswith(("sh", "sz", "bj")) and strip_prefix(code).startswith("3"))


def is_star(code):
    """是否科创板(STAR Market, sh688xxx)"""
    code = str(code)
    return code.startswith("sh68") or (not code.startswith(("sh", "sz", "bj")) and strip_prefix(code).startswith("68"))


# ══════════════════════════════════════════════
# 2. 涨停判定（v5.1 价格判定法）
# ══════════════════════════════════════════════

def get_limit_threshold(code, name=""):
    """
    返回该股票的涨跌停阈值（百分比浮点数）

    规则：
      ST/*ST   → 5%
      北交所    → 30%
      创业板    → 20%
      科创板    → 20%
      主板      → 10%
    """
    name = str(name) if name else ""
    code = str(code)

    if "ST" in name.upper():
        return 5.0

    if is_bj(code):
        return 30.0
    if is_gem(code) or is_star(code):
        return 20.0

    return 10.0


def _round_price(val):
    """
    交易所四舍五入（非银行家舍入）
    Python round() 用银行家舍入，交易所用传统四舍五入
    """
    return math.floor(val * 100 + 0.5) / 100


def calc_limit_price(prev_close, threshold):
    """
    计算涨停价/跌停价

    参数：
      prev_close — 昨收价
      threshold  — 涨跌停阈值百分比（如 10.0）

    返回：(涨停价, 跌停价) 元组
    """
    up = _round_price(prev_close * (1 + threshold / 100))
    down = _round_price(prev_close * (1 - threshold / 100))
    return up, down


def is_limit_up(code, name, chg_pct, prev_close=None, price=None):
    """
    该股票是否涨停

    v5.1: 优先用价格判定（最新价 >= 涨停价），fallback到涨跌幅判定
    价格判定需要 prev_close 和 price 参数

    注意：ST股由调用方决定是否排除，此函数只做判定不做过滤
    """
    threshold = get_limit_threshold(code, name)

    # 优先：价格判定（精确）
    if prev_close is not None and price is not None:
        if prev_close <= 0 or price <= 0:
            return False
        limit_up_price, _ = calc_limit_price(prev_close, threshold)
        return price >= limit_up_price

    # fallback：涨跌幅判定（兼容无昨收的场景）
    if chg_pct is None or (isinstance(chg_pct, float) and math.isnan(chg_pct)):
        return False
    return chg_pct >= threshold - 0.2


def is_limit_down(code, name, chg_pct, prev_close=None, price=None):
    """
    该股票是否跌停

    v5.1: 优先用价格判定，fallback到涨跌幅判定
    """
    threshold = get_limit_threshold(code, name)

    if prev_close is not None and price is not None:
        if prev_close <= 0 or price <= 0:
            return False
        _, limit_down_price = calc_limit_price(prev_close, threshold)
        return price <= limit_down_price

    if chg_pct is None or (isinstance(chg_pct, float) and math.isnan(chg_pct)):
        return False
    return chg_pct <= -(threshold - 0.2)


def is_st(name):
    """是否ST/*ST股票"""
    return "ST" in str(name).upper() if name else False


# ══════════════════════════════════════════════
# 3. DataFrame过滤
# ══════════════════════════════════════════════

def _detect_columns(df):
    """
    自动检测DataFrame列名，返回 dict:
      code, name, chg, price, open, prev_close, volume
    缺失的列返回None
    """
    mapping = {}
    for col in df.columns:
        col_s = str(col)
        if "代码" in col_s and "code" not in mapping:
            mapping["code"] = col
        elif "名称" in col_s and "name" not in mapping:
            mapping["name"] = col
        elif "涨跌幅" in col_s and "chg" not in mapping:
            mapping["chg"] = col
        elif "最新价" in col_s and "price" not in mapping:
            mapping["price"] = col
        elif "今开" in col_s and "open" not in mapping:
            mapping["open"] = col
        elif "昨收" in col_s and "prev_close" not in mapping:
            mapping["prev_close"] = col
        elif "成交额" in col_s and "volume" not in mapping:
            mapping["volume"] = col
    return mapping


def filter_valid(df, exclude_bj=True, exclude_new=True, exclude_suspended=True):
    """
    统一过滤逻辑，返回过滤后的DataFrame副本

    参数：
      exclude_bj:        排除北交所（默认True）
      exclude_new:       排除新股 — 名称以N/C开头（默认True）
      exclude_suspended: 排除停牌 — 最新价<=0（默认True）

    自动检测列名，兼容各种DataFrame格式
    """
    cols = _detect_columns(df)
    result = df.copy()

    if exclude_suspended and cols.get("price"):
        result = result[result[cols["price"]] > 0]

    if cols.get("chg"):
        result = result[result[cols["chg"]].notna()]

    if exclude_bj and cols.get("code"):
        mask = result[cols["code"]].astype(str).str.startswith("bj")
        result = result[~mask]

    if exclude_new and cols.get("name"):
        mask = result[cols["name"]].astype(str).str.match(r"^[NC]")
        result = result[~mask]

    return result


def add_stripped_code(df):
    """
    给DataFrame加一列 '_code'（去前缀的6位代码）
    如果已有则覆盖
    返回带 _code 列的DataFrame副本
    """
    cols = _detect_columns(df)
    result = df.copy()
    if cols.get("code"):
        result["_code"] = result[cols["code"]].astype(str).apply(strip_prefix)
    return result


# ══════════════════════════════════════════════
# 4. 批量涨停统计
# ══════════════════════════════════════════════

def calc_limit_counts(df, exclude_bj=True):
    """
    统计涨停/跌停数量（排除ST，排除新股，与同花顺口径一致）

    v5.1: 使用价格判定法（最新价 >= 涨停价）
    口径：排除ST、排除北交所、包含新股

    参数：
      df: 全市场DataFrame（未过滤也可以，内部会过滤）
      exclude_bj: 是否排除北交所（默认True）

    返回：(limit_up, limit_down) 整数元组
    """
    valid = filter_valid(df, exclude_bj=exclude_bj, exclude_new=True, exclude_suspended=True)
    cols = _detect_columns(valid)

    if not cols.get("code") or not cols.get("chg"):
        return 0, 0

    col_code = cols["code"]
    col_chg = cols["chg"]
    col_name = cols.get("name")
    col_price = cols.get("price")
    col_prev = cols.get("prev_close")

    has_price_data = col_price is not None and col_prev is not None

    limit_up = 0
    limit_down = 0

    for _, row in valid.iterrows():
        try:
            code = str(row[col_code])
            name = str(row[col_name]) if col_name else ""
            chg = float(row[col_chg])

            if math.isnan(chg):
                continue

            # ST不计入涨停/跌停（与同花顺一致）
            if is_st(name):
                continue

            if has_price_data:
                price = float(row[col_price])
                prev = float(row[col_prev])
                if is_limit_up(code, name, chg, prev_close=prev, price=price):
                    limit_up += 1
                elif is_limit_down(code, name, chg, prev_close=prev, price=price):
                    limit_down += 1
            else:
                # fallback: 无昨收列时用涨跌幅判定
                if is_limit_up(code, name, chg):
                    limit_up += 1
                elif is_limit_down(code, name, chg):
                    limit_down += 1
        except (ValueError, TypeError):
            continue

    return limit_up, limit_down


def find_limit_up_codes(df, exclude_bj=True):
    """
    找出涨停股的代码集合（去前缀的6位代码）
    用于次日溢价率计算

    v5.1: 使用价格判定法，排除ST
    注意：溢价率场景需要排除ST（ST涨停不算入溢价率统计）

    返回：set of str (6位代码)
    """
    valid = filter_valid(df, exclude_bj=exclude_bj, exclude_new=True, exclude_suspended=True)
    cols = _detect_columns(valid)

    if not cols.get("code") or not cols.get("chg"):
        return set()

    col_code = cols["code"]
    col_chg = cols["chg"]
    col_name = cols.get("name")
    col_price = cols.get("price")
    col_prev = cols.get("prev_close")

    has_price_data = col_price is not None and col_prev is not None

    codes = set()
    for _, row in valid.iterrows():
        try:
            code = str(row[col_code])
            name = str(row[col_name]) if col_name else ""
            chg = float(row[col_chg])

            if math.isnan(chg):
                continue

            # ST不计入
            if is_st(name):
                continue

            if has_price_data:
                price = float(row[col_price])
                prev = float(row[col_prev])
                if is_limit_up(code, name, chg, prev_close=prev, price=price):
                    codes.add(strip_prefix(code))
            else:
                if is_limit_up(code, name, chg):
                    codes.add(strip_prefix(code))
        except (ValueError, TypeError):
            continue

    return codes


def calc_basic_counts(df, exclude_bj=True):
    """
    计算基础市场统计

    返回 dict:
      up_count, down_count, flat_count, total, up_ratio
    """
    valid = filter_valid(df, exclude_bj=exclude_bj, exclude_new=False, exclude_suspended=True)
    cols = _detect_columns(valid)

    if not cols.get("chg"):
        return {"up_count": 0, "down_count": 0, "flat_count": 0, "total": 0, "up_ratio": None}

    chg = valid[cols["chg"]].astype(float)
    up = int((chg > 0).sum())
    down = int((chg < 0).sum())
    flat = int((chg == 0).sum())
    total = up + down + flat

    return {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "total": total,
        "up_ratio": round(up / total * 100, 1) if total > 0 else None,
    }
