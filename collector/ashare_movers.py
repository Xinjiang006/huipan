"""
慧盘 · A股涨跌榜采集器
数据源：
  - AKShare stock_zh_a_spot() — Sina源全市场行情（替代东财 stock_zh_a_spot_em）
  - Sina批量接口 — A股主要指数
输出：static/data/ashare_movers.json
缓存：static/data/.spot_cache.pkl（供ashare_overview.py复用，避免重复调用51s接口）

v3.5新增：
  - save_yesterday_picks()：收盘后从spot数据提取Top100涨跌榜，存 yesterday_picks.json
  - load_sector_map()：读取 config/sector_map.json 板块映射（三层降级）

v3.5.1修复：
  - regime计算移到picks保存之前（先读昨日picks算T+1，再覆盖为今天）
  - 修复 collector.regime_collector import 路径（sys.path.insert项目根目录）
  - 每次fetch成功后自动归档pkl到archive/spot/（同日覆盖，确保至少有一份）

v3.6新增：
  - save_opening_picks()：09:28窗口内从spot数据提取竞价涨幅/跌幅Top100
  - opening_picks.json：盘中追踪模块(intraday_tracker.py)的数据源
  - 竞价涨幅 = (开盘价 - 昨收) / 昨收 × 100

v3.8.1修复：
  - 排除北交所9开头股票（±30%涨跌幅，混入主板排行扭曲数据）
  - picks新增is_new标记（N=首日/C=2-5日，无涨跌幅限制，regime计算时排除）
"""

import json
import os
import sys
import time
import pickle
import shutil
import requests
import math
from datetime import datetime

import akshare as ak
from sources.index import fetch_indices as _fetch_raw_indices
from sources.spot import fetch_spot as _source_fetch_spot

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CACHE_PATH = os.path.join(DATA_DIR, ".spot_cache.pkl")
OUTPUT_PATH = os.path.join(DATA_DIR, "ashare_movers.json")
PICKS_PATH = os.path.join(DATA_DIR, "yesterday_picks.json")
OPENING_PATH = os.path.join(DATA_DIR, "opening_picks.json")  # v3.6新增
PICKS_HISTORY_PATH = os.path.join(DATA_DIR, "picks_history.json")  # v4.2 气泡下钻
SECTOR_MAP_PATH = os.path.join(CONFIG_DIR, "sector_map.json")
INDEX_CONS_PATH = os.path.join(CONFIG_DIR, "index_constituents.json")  # 由ashare_overview.py生成

os.makedirs(DATA_DIR, exist_ok=True)

# 确保项目根目录在 sys.path 中（解决 from collector.xxx / from storage.xxx 的 import 问题）
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 股价档位定义（元）
PRICE_BINS = [0, 10, 30, 50, 100]  # 区间右边界，最后一档为100+


def clean_nan(obj):
    """递归清理NaN/Inf，替换为None"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 2)
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(i) for i in obj]
    return obj


def fetch_all_stocks():
    """获取全市场行情（通过Source层，含缓存）"""
    return _source_fetch_spot()


def fetch_indices():
    """获取A股主要指数（通过Source层，Sina优先Tencent降级）"""
    print("[ashare_movers] 获取指数...")
    KEY_MAP = {"sh000001": "sh", "sz399001": "sz", "sz399006": "cyb"}
    raw = _fetch_raw_indices()
    if not raw:
        return {}
    result = {}
    for k, v in raw.items():
        if k in KEY_MAP:
            result[KEY_MAP[k]] = {
                "name": v["name"],
                "value": v["price"],
                "change_pct": v["change_pct"],
            }
    print(f"  ✅ {len(result)}个指数")
    return result



def process_movers(df):
    """从全市场行情中提取涨跌榜+成交额榜"""
    # 容错列名匹配
    col_code = next((c for c in df.columns if "代码" in c), None)
    col_name = next((c for c in df.columns if "名称" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)
    col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
    col_amount = next((c for c in df.columns if "成交额" in c), None)

    if not all([col_code, col_name, col_price, col_chg]):
        print("  ❌ 列名匹配失败")
        return [], [], []

    # 过滤掉无效行（价格为0或NaN）
    valid = df[df[col_price] > 0].copy()

    # 代码去前缀（bj920000 → 920000）
    valid["_code"] = valid[col_code].str.replace(r"^[a-z]{2}", "", regex=True)

    # v3.8.1: 排除北交所（9开头，涨跌幅±30%，混入主板排行扭曲数据）
    # v3.9.2: 排除退市整理期股票（极端涨跌幅扭曲排行）
    valid = valid[~valid[col_name].str.contains("退", na=False)].copy()
    valid = valid[~valid["_code"].str.startswith("9")].copy()

    def to_item(row, include_amount=False):
        item = {
            "code": row["_code"],
            "name": row[col_name],
            "price": round(float(row[col_price]), 2),
            "change_pct": round(float(row[col_chg]), 2),
        }
        if include_amount and col_amount:
            # 成交额从元转亿元
            amt = float(row[col_amount]) / 1e8 if row[col_amount] else 0
            item["amount"] = round(amt, 2)
        return item

    # 涨幅前50
    gainers = valid.nlargest(50, col_chg)
    gainers_list = [to_item(row) for _, row in gainers.iterrows()]

    # 跌幅前50
    losers = valid.nsmallest(50, col_chg)
    losers_list = [to_item(row) for _, row in losers.iterrows()]

    # 成交额前50
    if col_amount:
        volume = valid.nlargest(50, col_amount)
        volume_list = [to_item(row, include_amount=True) for _, row in volume.iterrows()]
    else:
        volume_list = []

    return gainers_list, losers_list, volume_list


# ─── 板块映射（v3.5新增） ───

def load_sector_map():
    """
    读取 config/sector_map.json 板块映射
    返回 dict: code(6位str) → sector_name(str)
    文件不存在时返回空dict，不抛出异常
    """
    if not os.path.exists(SECTOR_MAP_PATH):
        print(f"  ⚠️ sector_map.json不存在（{SECTOR_MAP_PATH}），板块字段将显示'未知'")
        print(f"     请先运行: python3 collector/update_sector_map.py")
        return {}
    try:
        with open(SECTOR_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = data.get("map", {})
        print(f"  ✅ 已加载板块映射({len(mapping)}只股票，更新于{data.get('updated_at','未知')})")
        return mapping
    except Exception as e:
        print(f"  ⚠️ sector_map.json读取失败: {e}，板块字段将显示'未知'")
        return {}


def load_index_cons_map():
    """
    读取 config/index_constituents.json（由ashare_overview.py生成）
    返回 dict: code(6位str) → cap_key("large"/"mid"/"small")
    其余股票默认"micro"
    文件不存在时返回空dict，build_picks_from_df里会全部归为"微盘"
    """
    if not os.path.exists(INDEX_CONS_PATH):
        print(f"  ⚠️ index_constituents.json不存在，请先运行ashare_overview.py")
        print(f"     picks的cap_label将全部显示'微盘'（数据偏差，待overview跑后自动修正）")
        return {}
    try:
        with open(INDEX_CONS_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        mapping = cache.get("code_to_cap", {})
        age_days = (time.time() - cache.get("time", 0)) / 86400
        print(f"  ✅ 成分股映射已加载({len(mapping)}只，{age_days:.0f}天前)")
        return mapping
    except Exception as e:
        print(f"  ⚠️ index_constituents.json读取失败: {e}")
        return {}


# cap_key → 中文标签
CAP_LABEL_MAP = {
    "large": "大盘",   # 沪深300
    "mid":   "中盘",   # 中证500
    "small":  "小盘",  # 中证1000
    "micro": "微盘",   # 其余
}

def get_cap_label(code, cons_map):
    """
    用指数成分股归属判断市值档位
    code: 6位字符串
    cons_map: load_index_cons_map() 返回的dict
    """
    cap_key = cons_map.get(str(code).zfill(6), "micro")
    return CAP_LABEL_MAP.get(cap_key, "微盘")


def get_price_label(price):
    """股价（元）→ 档位标签"""
    if price is None or math.isnan(price):
        return "未知"
    if price < 10:
        return "0-10"
    elif price < 30:
        return "10-30"
    elif price < 50:
        return "30-50"
    elif price < 100:
        return "50-100"
    else:
        return "100+"


# ─── yesterday_picks 采集（v3.5新增） ───

def load_spot_with_retry(max_retry=3, retry_interval=5):
    """
    从pkl缓存读取spot数据（带重试）
    ashare_movers主流程已把df存入pkl，这里直接读
    Retry原因：pkl可能还在写入中（race condition）
    """
    for attempt in range(1, max_retry + 1):
        try:
            with open(CACHE_PATH, "rb") as f:
                cache = pickle.load(f)
            df = cache.get("df")
            if df is not None and len(df) > 0:
                age = time.time() - cache.get("time", 0)
                print(f"  ✅ pkl读取成功（{len(df)}只，缓存{age:.0f}s前）")
                return df
            else:
                raise ValueError("pkl内容为空")
        except Exception as e:
            print(f"  ⚠️ pkl读取第{attempt}次失败: {e}")
            if attempt < max_retry:
                time.sleep(retry_interval)
    print(f"  ❌ pkl读取失败（已重试{max_retry}次），跳过picks保存")
    return None


def is_market_closed():
    """
    判断当前是否为收盘后
    规则：当前时间 >= 15:00 或 当前时间 < 09:00（夜间/早晨）
    不依赖交易日历，简单可靠
    """
    now_time = datetime.now().time()
    from datetime import time as dt_time
    after_close = now_time >= dt_time(15, 0)
    before_open = now_time < dt_time(9, 0)
    return after_close or before_open


def build_picks_from_df(df, sector_map, cons_map, top_n=100):
    """
    从全市场DataFrame提取Top100涨幅/跌幅榜
    cap_label 使用指数成分股归属（不依赖市值列）
    返回 (gainers_list, losers_list) 或 (None, None) 如果失败
    """
    col_code   = next((c for c in df.columns if "代码" in c), None)
    col_name   = next((c for c in df.columns if "名称" in c), None)
    col_price  = next((c for c in df.columns if "最新价" in c), None)
    col_chg    = next((c for c in df.columns if "涨跌幅" in c), None)

    if not all([col_code, col_name, col_price, col_chg]):
        print("  ❌ build_picks: 必要列名匹配失败")
        return None, None

    valid = df[df[col_price] > 0].copy()
    valid["_code"] = valid[col_code].str.replace(r"^[a-z]{2}", "", regex=True)

    # v3.8.1: 排除北交所（9开头）
    valid = valid[~valid["_code"].str.startswith("9")].copy()
    # v3.9.2: 排除退市整理期股票
    valid = valid[~valid[col_name].str.contains("退", na=False)].copy()

    def to_pick(row):
        code = row["_code"]
        name = str(row[col_name])
        price = float(row[col_price])
        return {
            "code": code,
            "name": name,
            "change_pct": round(float(row[col_chg]), 2),
            "price": round(price, 2),
            "cap_label": get_cap_label(code, cons_map),   # 成分股归属
            "price_label": get_price_label(price),
            "sector": sector_map.get(code, "未知"),
            # v3.8.1: 标记新股（N=首日/C=2-5日，无涨跌幅限制，regime计算时排除）
            "is_new": name.startswith("N") or name.startswith("C"),
        }

    gainers_df = valid.nlargest(top_n, col_chg)
    losers_df  = valid.nsmallest(top_n, col_chg)

    gainers = [to_pick(row) for _, row in gainers_df.iterrows()]
    losers  = [to_pick(row) for _, row in losers_df.iterrows()]

    return gainers, losers


def save_yesterday_picks(df, sector_map, cons_map):
    """
    从全市场spot数据提取Top100涨/跌榜，写入 yesterday_picks.json

    两种运行状态：
    - 盘中（09:00-14:59）：is_final=False，计算但【不写文件】（中间状态，备用）
    - 收盘后（≥15:00 或 <09:00）：is_final=True，写文件

    写文件流程：
    1. 先备份旧文件为 .bak
    2. 写入临时文件 .tmp
    3. 原子 rename：tmp → json
    """
    print("[ashare_movers] 生成yesterday_picks...")

    # 1. 提取数据
    gainers, losers = build_picks_from_df(df, sector_map, cons_map)
    if gainers is None:
        print("  ❌ picks提取失败，跳过")
        return

    is_final = is_market_closed()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = datetime.now().strftime("%Y-%m-%d")

    picks_data = {
        "date": today_str,
        "saved_at": now_str,
        "is_final": is_final,
        "top100_gainers": gainers,
        "top100_losers": losers,
    }
    picks_data = clean_nan(picks_data)

    if not is_final:
        print(f"  ℹ️ 当前为盘中状态（{datetime.now().strftime('%H:%M')}），picks已计算但不写文件（is_final=False）")
        print(f"     涨幅Top{len(gainers)}只，跌幅Top{len(losers)}只")
        return picks_data  # 盘中返回数据，供未来实时预览用

    # 2. 写文件（收盘后）
    tmp_path = PICKS_PATH + ".tmp"
    bak_path = PICKS_PATH + ".bak"

    try:
        # 备份旧文件
        if os.path.exists(PICKS_PATH):
            try:
                import shutil
                shutil.copy2(PICKS_PATH, bak_path)
            except Exception as e:
                print(f"  ⚠️ 备份失败（不影响写入）: {e}")

        # 写临时文件
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(picks_data, f, ensure_ascii=False, indent=2)

        # 原子rename
        os.replace(tmp_path, PICKS_PATH)

        print(f"  ✅ yesterday_picks已保存（涨Top{len(gainers)}/跌Top{len(losers)}）→ {PICKS_PATH}")

        # v4.2: 同步更新picks_history（气泡图下钻数据）
        try:
            _update_picks_history(picks_data)
        except Exception as e:
            print(f"  ⚠️ picks_history更新失败: {e}")

    except Exception as e:
        print(f"  ❌ picks写入失败: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    return picks_data


def _update_picks_history(picks_data, max_days=5):
    """
    v4.2: 维护滚动5天picks明细，供前端气泡图下钻使用
    结构：{ "2026-03-30": {"top100_gainers": [...], "top100_losers": [...]}, ... }
    """
    history = {}
    if os.path.exists(PICKS_HISTORY_PATH):
        try:
            with open(PICKS_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {}

    date_key = picks_data["date"]
    history[date_key] = {
        "top100_gainers": picks_data["top100_gainers"],
        "top100_losers": picks_data["top100_losers"],
    }

    # 只保留最近 max_days 天
    sorted_dates = sorted(history.keys(), reverse=True)
    for d in sorted_dates[max_days:]:
        del history[d]

    # 原子写入
    tmp = PICKS_HISTORY_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        os.replace(tmp, PICKS_HISTORY_PATH)
        print(f"  ✅ picks_history已更新（{len(history)}天）→ {PICKS_HISTORY_PATH}")
    except Exception as e:
        print(f"  ⚠️ picks_history写入失败: {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


# ─── opening_picks 采集（v3.6新增） ───

def is_opening_window():
    """
    判断当前是否在竞价窗口内（09:00-09:35）
    09:28采集，留5分钟缓冲
    """
    now_time = datetime.now().time()
    from datetime import time as dt_time
    return dt_time(9, 0) <= now_time < dt_time(9, 35)


def save_opening_picks(df, sector_map, cons_map, top_n=100):
    """
    v3.6新增：从spot数据提取今日竞价涨幅/跌幅Top100
    竞价涨幅 = (开盘价 - 昨收) / 昨收 × 100
    写入 opening_picks.json（每天覆盖，不归档）

    仅在 is_opening_window() 为True时调用（09:00-09:35）
    """
    print("[ashare_movers] 生成opening_picks（竞价Top100）...")

    col_code  = next((c for c in df.columns if "代码" in c), None)
    col_name  = next((c for c in df.columns if "名称" in c), None)
    col_price = next((c for c in df.columns if "最新价" in c), None)
    col_open  = next((c for c in df.columns if "开盘价" in c or "今开" in c), None)
    col_close = next((c for c in df.columns if "昨收" in c), None)

    if not all([col_code, col_name, col_price]):
        print("  ❌ opening_picks: 必要列名匹配失败")
        return

    # 开盘价和昨收可能列名不同，尝试多种匹配
    if col_open is None:
        # stock_zh_a_spot() 可能用"今开"
        for c in df.columns:
            if "今开" in c or "开盘" in c:
                col_open = c
                break
    if col_close is None:
        for c in df.columns:
            if "昨收" in c or "昨日" in c:
                col_close = c
                break

    if not col_open or not col_close:
        print(f"  ⚠️ opening_picks: 缺少开盘价({col_open})或昨收({col_close})列")
        print(f"     可用列名: {list(df.columns)}")
        print(f"  → 降级方案：使用当前涨跌幅替代竞价涨幅")
        # 降级：用当前涨跌幅（盘初基本等于竞价涨幅）
        _save_opening_fallback(df, sector_map, cons_map, top_n)
        return

    valid = df[df[col_price] > 0].copy()
    valid["_code"] = valid[col_code].str.replace(r"^[a-z]{2}", "", regex=True)

    # v3.8.1: 排除北交所（9开头）
    valid = valid[~valid["_code"].str.startswith("9")].copy()
    # v3.9.2: 排除退市整理期股票
    valid = valid[~valid[col_name].str.contains("退", na=False)].copy()

    # 计算竞价涨幅
    valid["_open"] = valid[col_open].apply(lambda x: float(x) if x and not math.isnan(float(x)) else 0)
    valid["_yclose"] = valid[col_close].apply(lambda x: float(x) if x and not math.isnan(float(x)) else 0)
    valid["_open_chg"] = valid.apply(
        lambda r: round((r["_open"] - r["_yclose"]) / r["_yclose"] * 100, 2)
        if r["_yclose"] > 0 and r["_open"] > 0 else 0,
        axis=1
    )

    # 过滤掉开盘价为0的（未开盘/停牌）
    tradable = valid[valid["_open"] > 0].copy()

    def to_pick(row, chg_col="_open_chg"):
        code = row["_code"]
        name = str(row[col_name])
        price = float(row[col_price])
        return {
            "code": code,
            "name": name,
            "change_pct": round(float(row[chg_col]), 2),
            "price": round(price, 2),
            "cap_label": get_cap_label(code, cons_map),
            "price_label": get_price_label(price),
            "sector": sector_map.get(code, "未知"),
            # v3.8.1: 标记新股
            "is_new": name.startswith("N") or name.startswith("C"),
        }

    # 涨幅Top100
    bull_df = tradable.nlargest(top_n, "_open_chg")
    bull_list = [to_pick(row) for _, row in bull_df.iterrows()]

    # 跌幅Top100
    bear_df = tradable.nsmallest(top_n, "_open_chg")
    bear_list = [to_pick(row) for _, row in bear_df.iterrows()]

    _write_opening_json(bull_list, bear_list, "opening_change")


def _save_opening_fallback(df, sector_map, cons_map, top_n=100):
    """降级方案：用当前涨跌幅替代竞价涨幅"""
    gainers, losers = build_picks_from_df(df, sector_map, cons_map, top_n)
    if gainers is None:
        print("  ❌ opening_picks降级也失败")
        return
    _write_opening_json(gainers, losers, "current_change_fallback")


def _write_opening_json(bull_list, bear_list, calc_method):
    """写入 opening_picks.json"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = datetime.now().strftime("%Y-%m-%d")

    data = {
        "date": today_str,
        "saved_at": now_str,
        "is_final": True,
        "calc_method": calc_method,
        "top100_gainers": bull_list,
        "top100_losers": bear_list,
    }
    data = clean_nan(data)

    try:
        with open(OPENING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  ✅ opening_picks已保存（涨Top{len(bull_list)}/跌Top{len(bear_list)}）"
              f"  calc={calc_method}")
    except Exception as e:
        print(f"  ❌ opening_picks写入失败: {e}")


# ─── 主采集函数 ───

def collect_ashare_movers():
    """主采集函数"""
    df = fetch_all_stocks()
    indices = fetch_indices()
    gainers, losers, volume = process_movers(df)

    now = datetime.now()
    data = {
        "date": now.strftime("%Y-%m-%d"),
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices,
        "gainers": gainers,
        "losers": losers,
        "volume": volume,
    }

    data = clean_nan(data)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[ashare_movers] → {OUTPUT_PATH}")
    print(f"  涨幅榜{len(gainers)}只, 跌幅榜{len(losers)}只, 成交额榜{len(volume)}只")

    # --- v3.5.1: 归档pkl（每次成功都存，同日覆盖，最后一次=最终版）---
    try:
        archive_dir = os.path.join(BASE_DIR, "static", "data", "archive", "spot")
        os.makedirs(archive_dir, exist_ok=True)
        archive_name = f"spot_{now.strftime('%Y%m%d')}.pkl"
        shutil.copy2(CACHE_PATH, os.path.join(archive_dir, archive_name))
        print(f"  📦 pkl已归档 → archive/spot/{archive_name}")
    except Exception as e:
        print(f"  ⚠️ pkl归档失败（不影响主流程）: {e}")

    # --- DuckDB 入库 ---
    try:
        from storage.duckdb_v3_tables import DuckDBV3Store
        store = DuckDBV3Store(os.path.join(BASE_DIR, "data", "huipan.duckdb"))
        store.save_ashare_movers(data['date'], data)
    except Exception as e:
        print(f"  ⚠️ DuckDB入库跳过: {e}")

    # --- v3.5: regime计算（先读昨日picks + 今日spot → 算T+1收益）---
    # ⚠️ 必须在 save_yesterday_picks 之前！否则picks被今天覆盖，永远算不出T+1
    try:
        from collector.regime_collector import collect_regime
        collect_regime()
    except Exception as e:
        print(f"  ⚠️ regime计算跳过: {e}")

    # --- v3.5: 保存yesterday_picks（regime算完后才覆盖，供明日regime使用）---
    # v3.9.2: 调度器15:10通过 HUIPAN_SKIP_PICKS=1 延迟保存，确保intraday先用昨天picks
    if os.environ.get("HUIPAN_SKIP_PICKS") != "1" and datetime.now().hour >= 15:
        try:
            sector_map = load_sector_map()
            cons_map = load_index_cons_map()
            save_yesterday_picks(df, sector_map, cons_map)
        except Exception as e:
            print(f"  ⚠️ yesterday_picks保存跳过: {e}")
    else:
        print(f"  ℹ️ SKIP_PICKS=1，picks稍后由调度器单独保存")

    # --- v3.6: 保存opening_picks（仅竞价窗口09:00-09:35内执行）---
    try:
        if is_opening_window():
            # sector_map/cons_map 可能已在上面加载，但如果yesterday_picks失败则没有
            if 'sector_map' not in dir() or 'cons_map' not in dir():
                sector_map = load_sector_map()
                cons_map = load_index_cons_map()
            save_opening_picks(df, sector_map, cons_map)
        else:
            print(f"  ℹ️ 非竞价窗口（{now.strftime('%H:%M')}），跳过opening_picks")
    except Exception as e:
        print(f"  ⚠️ opening_picks保存跳过: {e}")

    return data


def save_picks_standalone():
    """
    v3.9.2: 独立保存yesterday_picks（从.spot_cache.pkl加载df）
    供调度器在intraday之后单独调用：python -m collector.ashare_movers --save-picks
    """
    print("[ashare_movers] 独立保存yesterday_picks...")
    if not os.path.exists(CACHE_PATH):
        print(f"  ❌ pkl不存在: {CACHE_PATH}")
        return
    try:
        with open(CACHE_PATH, "rb") as f:
            cache = pickle.load(f)
        df = cache.get("df")
        if df is None or df.empty:
            print("  ❌ pkl中无有效数据")
            return
        sector_map = load_sector_map()
        cons_map = load_index_cons_map()
        save_yesterday_picks(df, sector_map, cons_map)
    except Exception as e:
        print(f"  ❌ 独立保存picks失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    if "--save-picks" in sys.argv:
        save_picks_standalone()
    else:
        collect_ashare_movers()
