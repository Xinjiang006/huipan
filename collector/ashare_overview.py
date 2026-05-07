"""
慧盘 · A股概况KPI采集器
数据源（全部非东财）：
  - AKShare stock_zh_a_spot() — Sina源，复用ashare_movers缓存
  - AKShare stock_rank_lxsz_ths() / lxxd_ths() — 同花顺连涨连跌
  - AKShare stock_board_industry_summary_ths() — 同花顺板块汇总
  - AKShare fund_etf_spot_ths() — 同花顺ETF代码列表
  - Sina批量接口 — 大中小微盘指数 + ETF实时行情
  - 同花顺网页 — 创新高/新低（月/年/历史 三周期）
输出：static/data/ashare_overview.json
注意：refresh.sh中应先跑ashare_movers.py（缓存全市场数据），再跑本脚本
v3.1: 新增全市场涨跌分布 (market_distribution)
"""

import json
import os
import sys
import time
import pickle
import math
import glob
import requests
from datetime import datetime
from io import StringIO
import re as _re

import pandas as pd
from sources.index import fetch_indices as _fetch_raw_indices
from sources.spot import load_spot as _source_load_spot
from sources.ths import fetch_consecutive, fetch_new_highs_lows
from sources.ths import fetch_sectors, fetch_etfs
from sources.constituents import load_index_constituents

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
CACHE_PATH = os.path.join(DATA_DIR, ".spot_cache.pkl")
OUTPUT_PATH = os.path.join(DATA_DIR, "ashare_overview.json")       # 收盘归档（≥15:00写入，写完不动）
LIVE_OUTPUT_PATH = os.path.join(DATA_DIR, "ashare_overview_live.json")  # 盘中实时（每次覆盖）

CACHE_TTL = 600  # 10分钟内复用缓存


def clean_nan(obj):
    """递归清理NaN/Inf"""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 2)
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(i) for i in obj]
    return obj


# ─── v3.10: 新高新低明细辅助函数 ───

def _load_label_maps():
    """加载板块映射和成分股归属，返回 (sector_map, cap_map)
    sector_map: {code: sector_name}
    cap_map: {code: cap_label}
    """
    sector_map = {}
    cap_map = {}

    # 板块映射
    sm_path = os.path.join(BASE_DIR, "config", "sector_map.json")
    if os.path.exists(sm_path):
        try:
            with open(sm_path, "r", encoding="utf-8") as f:
                sm = json.load(f)
            sector_map = sm.get("map", {})
        except Exception:
            pass

    # 成分股归属 → cap_label
    ic_path = os.path.join(BASE_DIR, "config", "index_constituents.json")
    if os.path.exists(ic_path):
        try:
            with open(ic_path, "r", encoding="utf-8") as f:
                ic = json.load(f)
            # 适配 code_to_cap 格式 {code: "large"/"mid"/"small"} → 中文标签
            EN_TO_CN = {"large": "大盘", "mid": "中盘", "small": "小盘"}
            code_to_cap = ic.get("code_to_cap", {})
            for code, en_label in code_to_cap.items():
                cap_map[str(code)] = EN_TO_CN.get(en_label, "微盘")
        except Exception:
            pass

    return sector_map, cap_map


def _rebuild_yesterday_zt_from_pkl():
    """从昨日归档pkl重建涨停代码列表（连板存活fallback）
    返回: set of 6位代码，或 None（无可用pkl）
    """
    archive_dir = os.path.join(DATA_DIR, "archive", "spot")
    if not os.path.exists(archive_dir):
        print(f"    归档目录不存在: {archive_dir}")
        return None

    today_str = datetime.now().strftime("%Y%m%d")
    pkls = sorted(glob.glob(os.path.join(archive_dir, "spot_*.pkl")), reverse=True)
    yd_path = None
    for p in pkls:
        pkl_date = os.path.basename(p).replace("spot_", "").replace(".pkl", "")
        if pkl_date < today_str:
            yd_path = p
            break
    if yd_path is None:
        print(f"    无昨日归档pkl")
        return None

    try:
        with open(yd_path, "rb") as f:
            data = pickle.load(f)
        df = data["df"] if isinstance(data, dict) and "df" in data else data
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
        col_code = next((c for c in df.columns if "代码" in c), None)
        col_name = next((c for c in df.columns if "名称" in c), None)
        if not col_chg or not col_code:
            return None

        codes = set()
        for _, row in df.iterrows():
            code = str(row[col_code])
            chg = float(row[col_chg]) if pd.notna(row[col_chg]) else 0
            name = str(row[col_name]) if col_name else ""
            if "ST" in name or "st" in name:
                continue
            if code.startswith("bj"):
                threshold = 29.5
            elif code.startswith(("sz30", "sh68")):
                threshold = 19.5
            else:
                threshold = 9.9
            if chg >= threshold:
                codes.add(code[-6:] if len(code) > 6 else code)

        print(f"    从{os.path.basename(yd_path)}重建: {len(codes)}只涨停")

        # 顺便补建 yesterday_limit_up.json
        pkl_date_str = os.path.basename(yd_path).replace("spot_", "").replace(".pkl", "")
        formatted_date = f"{pkl_date_str[:4]}-{pkl_date_str[4:6]}-{pkl_date_str[6:]}"
        lu_path = os.path.join(DATA_DIR, "yesterday_limit_up.json")
        tmp = lu_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": formatted_date, "codes": list(codes)}, f, ensure_ascii=False)
        os.replace(tmp, lu_path)
        print(f"    已补建 yesterday_limit_up.json (date={formatted_date})")

        return codes
    except Exception as e:
        print(f"    归档pkl重建失败: {e}")
        return None


def load_spot_data():
    """加载全市场行情（通过Source层，含缓存）"""
    return _source_load_spot()


def calc_kpi(df):
    """从全市场行情计算KPI（含炸板率检测）"""
    col_price = next((c for c in df.columns if "最新价" in c), None)
    col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
    col_amount = next((c for c in df.columns if "成交额" in c), None)
    col_code = next((c for c in df.columns if "代码" in c), None)
    col_name = next((c for c in df.columns if "名称" in c), None)
    col_high = next((c for c in df.columns if "最高" in c), None)
    col_yclose = next((c for c in df.columns if "昨收" in c), None)

    valid = df[df[col_price] > 0].copy()

    # 成交额（元→亿元）
    volume_total = round(valid[col_amount].sum() / 1e8, 1) if col_amount else 0

    # 涨跌统计
    up_count = int((valid[col_chg] > 0).sum())
    down_count = int((valid[col_chg] < 0).sum())
    flat_count = int((valid[col_chg] == 0).sum())
    stock_count = len(valid)

    # 涨停/跌停判定 + 炸板检测
    limit_up = 0
    limit_down = 0
    zha_ban_count = 0
    limit_up_codes = []  # 今日涨停代码列表（供连板计算）

    for _, row in valid.iterrows():
        code = str(row[col_code])
        chg = float(row[col_chg])
        name = str(row[col_name]) if col_name else ""

        # ST股票不计入涨停/跌停/炸板
        is_st = "ST" in name or "st" in name
        if is_st:
            continue

        if code.startswith(("bj",)):
            threshold = 29.5
            limit_mult = 1.30
        elif code.startswith(("sz30", "sh68")):
            threshold = 19.5
            limit_mult = 1.20
        else:
            threshold = 9.9
            limit_mult = 1.10

        if chg >= threshold:
            limit_up += 1
            # 记录6位纯数字代码
            limit_up_codes.append(code[-6:] if len(code) > 6 else code)
        elif chg <= -threshold:
            limit_down += 1
        elif col_high and col_yclose:
            # 炸板检测：最高价触及涨停价但收盘没封住
            try:
                high = float(row[col_high]) if pd.notna(row[col_high]) else 0
                yclose = float(row[col_yclose]) if pd.notna(row[col_yclose]) else 0
                if yclose > 0 and high > 0:
                    limit_price = round(yclose * limit_mult, 2)
                    if high >= limit_price:
                        zha_ban_count += 1
            except (ValueError, TypeError):
                pass

    # 炸板率 = 炸板数 / (涨停 + 炸板) * 100
    total_touched = limit_up + zha_ban_count
    zha_ban_rate = round(zha_ban_count / total_touched * 100, 1) if total_touched > 0 else 0

    # --- 连板存活率：加载昨日涨停代码对比 ---
    lianban_survived = None
    lianban_total = None
    yesterday_lu_path = os.path.join(DATA_DIR, "yesterday_limit_up.json")
    try:
        yesterday_codes = None

        if os.path.exists(yesterday_lu_path):
            with open(yesterday_lu_path, "r", encoding="utf-8") as f:
                ylu = json.load(f)
            # 确保不是同一天的数据
            if ylu.get("date") != datetime.now().strftime("%Y-%m-%d"):
                yesterday_codes = set(ylu.get("codes", []))
            else:
                print(f"  ℹ️ yesterday_limit_up.json是今天的数据，跳过连板计算")
        else:
            # fallback: 从昨日归档pkl重建涨停代码
            print(f"  ⚠️ yesterday_limit_up.json不存在，尝试从归档pkl重建...")
            yesterday_codes = _rebuild_yesterday_zt_from_pkl()

        if yesterday_codes is not None and len(yesterday_codes) > 0:
            today_lu_set = set(limit_up_codes)
            lianban_total = len(yesterday_codes)
            lianban_survived = len(yesterday_codes & today_lu_set)
            print(f"  ✅ 连板存活: {lianban_survived}/{lianban_total} "
                  f"({round(lianban_survived/lianban_total*100)}%)" if lianban_total > 0
                  else f"  ℹ️ 昨日无涨停")
        elif yesterday_codes is not None and len(yesterday_codes) == 0:
            print(f"  ℹ️ 昨日无涨停代码")
    except Exception as e:
        print(f"  ⚠️ 连板存活计算失败: {e}")

    # 保存今日涨停代码（供明日连板计算）
    # 仅在收盘后（15:00之后）保存，避免盘中炸板股污染数据
    now = datetime.now()
    if now.hour >= 15:
        try:
            today_str = now.strftime("%Y-%m-%d")
            tmp_path = yesterday_lu_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"date": today_str, "codes": limit_up_codes}, f, ensure_ascii=False)
            os.replace(tmp_path, yesterday_lu_path)
        except Exception as e:
            print(f"  ⚠️ 涨停代码保存失败: {e}")

    result = {
        "volume_total": volume_total,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "stock_count": stock_count,
        "zha_ban_count": zha_ban_count,
        "zha_ban_rate": zha_ban_rate,
    }
    if lianban_survived is not None:
        result["lianban_survived"] = lianban_survived
        result["lianban_total"] = lianban_total

    return result


def calc_cap_distribution(df):
    """按指数成分股分4档，每档按涨跌幅分桶统计
    大盘=沪深300, 中盘=中证500, 小盘=中证1000, 微盘=其余
    """
    col_price = next((c for c in df.columns if "最新价" in c), None)
    col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
    col_code = next((c for c in df.columns if "代码" in c), None)

    if not col_code or not col_chg:
        print("  ⚠️ 缺少代码或涨跌幅列，跳过分布计算")
        return {}

    valid = df[df[col_price] > 0].copy()
    valid["chg"] = pd.to_numeric(valid[col_chg], errors="coerce")
    valid = valid.dropna(subset=["chg"])

    code_to_cap = load_index_constituents()
    if not code_to_cap:
        print("  ⚠️ 成分股数据为空，跳过分布计算")
        return {}

    valid["cap_group"] = valid[col_code].apply(
        lambda c: code_to_cap.get(str(c)[-6:], "micro")
    )

    BIN_DEFS = [
        ">7%", "5~7%", "3~5%", "1~3%", "0~1%", "0%",
        "-1~0%", "-3~-1%", "-5~-3%", "-7~-5%", "<-7%",
    ]

    def classify_bin(chg):
        if chg > 7:    return ">7%"
        if chg > 5:    return "5~7%"
        if chg > 3:    return "3~5%"
        if chg > 1:    return "1~3%"
        if chg > 0:    return "0~1%"
        if chg == 0:   return "0%"
        if chg > -1:   return "-1~0%"
        if chg > -3:   return "-3~-1%"
        if chg > -5:   return "-5~-3%"
        if chg > -7:   return "-7~-5%"
        return "<-7%"

    result = {}
    for cap_key in ["large", "mid", "small", "micro"]:
        subset = valid[valid["cap_group"] == cap_key]
        bins = {b: 0 for b in BIN_DEFS}
        for chg in subset["chg"]:
            bins[classify_bin(chg)] += 1
        result[cap_key] = [{"label": b, "count": bins[b]} for b in BIN_DEFS]

    counts = {k: len(valid[valid["cap_group"] == k]) for k in ["large", "mid", "small", "micro"]}
    print(f"  ✅ 涨跌分布: 大{counts['large']} 中{counts['mid']} 小{counts['small']} 微{counts['micro']}")
    return result


def calc_market_distribution(df):
    """全市场涨跌幅分布，11个桶（用于独立弹窗）"""
    col_price = next((c for c in df.columns if "最新价" in c), None)
    col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
    if not col_chg:
        return []

    valid = df[df[col_price] > 0].copy() if col_price else df.copy()
    values = pd.to_numeric(valid[col_chg], errors="coerce").dropna()

    BIN_DEFS = [
        (">7%",    lambda v: v > 7),
        ("5~7%",   lambda v: 5 < v <= 7),
        ("3~5%",   lambda v: 3 < v <= 5),
        ("1~3%",   lambda v: 1 < v <= 3),
        ("0~1%",   lambda v: 0 < v <= 1),
        ("0%",     lambda v: v == 0),
        ("-1~0%",  lambda v: -1 <= v < 0),
        ("-3~-1%", lambda v: -3 <= v < -1),
        ("-5~-3%", lambda v: -5 <= v < -3),
        ("-7~-5%", lambda v: -7 <= v < -5),
        ("<-7%",   lambda v: v < -7),
    ]

    result = []
    for label, cond in BIN_DEFS:
        count = int(values.apply(cond).sum())
        result.append({"label": label, "count": count})

    total = sum(b["count"] for b in result)
    print(f"  ✅ 全市场分布: {total}只")
    return result


def fetch_cap_indices():
    """获取大中小微盘指数（通过Source层，Sina优先Tencent降级）"""
    print("  获取大中小微盘指数...")
    CAP_CODES = ["sh000300", "sh000905", "sh000852", "sz399303"]
    CAP_MAP = {
        "sh000300": ("large", "沪深300", "000300"),
        "sh000905": ("mid",   "中证500", "000905"),
        "sh000852": ("small", "中证1000", "000852"),
        "sz399303": ("micro", "国证2000", "399303"),
    }
    raw = _fetch_raw_indices(CAP_CODES)
    if not raw:
        return {}
    result = {}
    for k, v in raw.items():
        if k in CAP_MAP:
            key, name, idx_code = CAP_MAP[k]
            result[key] = {
                "name": name,
                "code": idx_code,
                "value": v["price"],
                "change_pct": v["change_pct"],
            }
    print(f"  ✅ {len(result)}个指数")
    return result


def calc_sector_streaks(sector_names):
    """从DuckDB ashare_sector_daily查最近5天数据，计算每个板块连涨/连跌天数
    返回: {sector_name: streak_int} 正=连涨, 负=连跌
    """
    streaks = {}
    try:
        import duckdb
        db_path = os.path.join(BASE_DIR, "data", "huipan.duckdb")
        if not os.path.exists(db_path):
            return streaks

        con = duckdb.connect(db_path, read_only=True)

        # 检查表是否存在
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name='ashare_sector_daily'"
        ).fetchall()]
        if not tables:
            con.close()
            return streaks

        # 取最近5个交易日的数据
        rows = con.execute("""
            SELECT date, name, change_pct
            FROM ashare_sector_daily
            WHERE date >= (SELECT MAX(date) - INTERVAL '10 days' FROM ashare_sector_daily)
            ORDER BY date DESC
        """).fetchall()
        con.close()

        if not rows:
            return streaks

        # 按板块分组，按日期降序
        from collections import defaultdict
        sector_days = defaultdict(list)
        for date, name, chg in rows:
            sector_days[name].append(chg)

        # 计算streak（从最近一天往前数连续同方向）
        for name in sector_names:
            days = sector_days.get(name, [])
            if not days or len(days) < 2:
                continue
            # days[0] = 今天（不算，还没收完或刚收），从days[1]开始算
            # 实际上如果今天数据已入库，days[0]就是今天
            # 我们从第一个开始算方向
            first = days[0]
            if first is None or first == 0:
                continue
            direction = 1 if first > 0 else -1
            streak = 1
            for d in days[1:]:
                if d is None:
                    break
                if (direction > 0 and d > 0) or (direction < 0 and d < 0):
                    streak += 1
                else:
                    break
            streaks[name] = streak * direction

        print(f"  ✅ 板块streak: {sum(1 for v in streaks.values() if abs(v) >= 2)}个连涨/连跌≥2天")
    except ImportError:
        pass
    except Exception as e:
        print(f"  ⚠️ 板块streak计算失败: {e}")
    return streaks


def save_kpi_to_duckdb(kpi, date_str):
    """将KPI快照存入DuckDB kpi_daily_snapshot表（用于30日排名）"""
    try:
        import duckdb
        db_path = os.path.join(BASE_DIR, "data", "huipan.duckdb")
        if not os.path.exists(db_path):
            print(f"  ⚠️ DuckDB不存在({db_path})，跳过快照")
            return

        con = duckdb.connect(db_path)

        # 确保表存在
        con.execute("""
            CREATE TABLE IF NOT EXISTS kpi_daily_snapshot (
                date DATE PRIMARY KEY,
                volume_total DOUBLE,
                up_count INTEGER,
                down_count INTEGER,
                flat_count INTEGER,
                limit_up INTEGER,
                limit_down INTEGER,
                consecutive_up_3 INTEGER,
                consecutive_down_3 INTEGER,
                high_month INTEGER,
                low_month INTEGER,
                high_year INTEGER,
                low_year INTEGER,
                high_ath INTEGER,
                low_ath INTEGER,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # v4.1: 新增炸板率+连板字段（兼容已有表）
        for col_def in [
            ("zha_ban_count", "INTEGER"),
            ("zha_ban_rate", "DOUBLE"),
            ("lianban_survived", "INTEGER"),
            ("lianban_total", "INTEGER"),
        ]:
            try:
                con.execute(f"ALTER TABLE kpi_daily_snapshot ADD COLUMN {col_def[0]} {col_def[1]}")
            except Exception:
                pass  # 列已存在

        # INSERT OR REPLACE
        con.execute("""
            INSERT OR REPLACE INTO kpi_daily_snapshot
                (date, volume_total, up_count, down_count, flat_count,
                 limit_up, limit_down, consecutive_up_3, consecutive_down_3,
                 high_month, low_month, high_year, low_year, high_ath, low_ath,
                 zha_ban_count, zha_ban_rate, lianban_survived, lianban_total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            date_str,
            kpi.get("volume_total", 0),
            kpi.get("up_count", 0),
            kpi.get("down_count", 0),
            kpi.get("flat_count", 0),
            kpi.get("limit_up", 0),
            kpi.get("limit_down", 0),
            kpi.get("consecutive_up_3", 0),
            kpi.get("consecutive_down_3", 0),
            kpi.get("high_month", 0),
            kpi.get("low_month", 0),
            kpi.get("high_year", 0),
            kpi.get("low_year", 0),
            kpi.get("high_ath", 0),
            kpi.get("low_ath", 0),
            kpi.get("zha_ban_count"),
            kpi.get("zha_ban_rate"),
            kpi.get("lianban_survived"),
            kpi.get("lianban_total"),
        ])

        # 导出最近30条到 kpi_history.json
        rows = con.execute("""
            SELECT date, volume_total, up_count, down_count, flat_count,
                   limit_up, limit_down, consecutive_up_3, consecutive_down_3,
                   high_month, low_month, high_year, low_year, high_ath, low_ath,
                   zha_ban_count, zha_ban_rate, lianban_survived, lianban_total
            FROM kpi_daily_snapshot
            ORDER BY date DESC
            LIMIT 30
        """).fetchall()

        history = []
        for r in rows:
            history.append({
                "date": str(r[0]),
                "volume_total": r[1],
                "up_count": r[2], "down_count": r[3], "flat_count": r[4],
                "limit_up": r[5], "limit_down": r[6],
                "consecutive_up_3": r[7], "consecutive_down_3": r[8],
                "high_month": r[9], "low_month": r[10],
                "high_year": r[11], "low_year": r[12],
                "high_ath": r[13], "low_ath": r[14],
                "zha_ban_count": r[15], "zha_ban_rate": r[16],
                "lianban_survived": r[17], "lianban_total": r[18],
            })

        con.close()

        hist_path = os.path.join(DATA_DIR, "kpi_history.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        print(f"  ✅ KPI快照已存DuckDB + 导出kpi_history.json ({len(history)}天)")

    except ImportError:
        print("  ⚠️ duckdb未安装，跳过KPI快照")
    except Exception as e:
        print(f"  ⚠️ KPI快照保存失败: {e}")


def save_new_high_low_to_duckdb(date_str, year_details):
    """v3.10: 将年度新高/新低个股明细存入DuckDB + 导出JSON
    year_details: [{"code","name","type","change_pct","price"}, ...]
    """
    try:
        import duckdb
        db_path = os.path.join(BASE_DIR, "data", "huipan.duckdb")
        if not os.path.exists(db_path):
            print(f"  ⚠️ DuckDB不存在({db_path})，跳过新高新低明细")
            return

        # 加载标签映射
        sector_map, cap_map = _load_label_maps()

        # 给每条记录打标签
        for rec in year_details:
            code = rec["code"]
            rec["sector"] = sector_map.get(code, "未知")
            rec["cap_label"] = cap_map.get(code, "微盘")

        con = duckdb.connect(db_path)

        # 确保表存在
        con.execute("""
            CREATE TABLE IF NOT EXISTS new_high_low_daily (
                date        DATE NOT NULL,
                code        VARCHAR NOT NULL,
                name        VARCHAR,
                type        VARCHAR NOT NULL,
                change_pct  DOUBLE,
                price       DOUBLE,
                cap_label   VARCHAR,
                sector      VARCHAR,
                PRIMARY KEY (date, code, type)
            )
        """)

        # 先删当天旧数据（支持盘中多次跑覆盖）
        con.execute(
            "DELETE FROM new_high_low_daily WHERE date = ?", [date_str]
        )

        # 批量INSERT
        count = 0
        for rec in year_details:
            con.execute("""
                INSERT INTO new_high_low_daily
                    (date, code, name, type, change_pct, price, cap_label, sector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                date_str, rec["code"], rec["name"], rec["type"],
                rec.get("change_pct"), rec.get("price"),
                rec.get("cap_label", "微盘"), rec.get("sector", "未知"),
            ])
            count += 1

        # ─── 导出 new_high_low.json ───
        # 今日板块聚合
        today_data = {}
        # ─── 价格分档定义（v5.8 新增）───
        PRICE_BUCKETS = [("0-10", 0, 10), ("10-30", 10, 30),
                         ("30-50", 30, 50), ("50-100", 50, 100),
                         ("100+", 100, float("inf"))]
        def _price_bucket(p):
            if p is None:
                return None
            for label, lo, hi in PRICE_BUCKETS:
                if lo <= p < hi:
                    return label
            return None

        for type_key in ("high_year", "low_year"):
            recs = [r for r in year_details if r["type"] == type_key]
            # 按板块聚合（v4.1: 含Top10个股明细）
            sector_stocks = {}
            cap_counts = {"微盘": 0, "小盘": 0, "中盘": 0, "大盘": 0}
            price_counts = {lb: 0 for lb, _, _ in PRICE_BUCKETS}  # v5.8 新增
            for r in recs:
                s = r.get("sector", "未知")
                sector_stocks.setdefault(s, []).append(r)
                cl = r.get("cap_label", "微盘")
                if cl in cap_counts:
                    cap_counts[cl] += 1
                pb = _price_bucket(r.get("price"))  # v5.8 新增
                if pb:
                    price_counts[pb] += 1
            by_sector = sorted(
                [{"sector": s, "count": len(stks),
                  "stocks": [{"name": st["name"], "code": st["code"],
                              "chg": round(st["change_pct"], 2) if st.get("change_pct") is not None else None}
                             for st in sorted(stks, key=lambda x: -(x.get("change_pct") or 0))]}
                 for s, stks in sector_stocks.items()],
                key=lambda x: -x["count"]
            )
            today_data[type_key] = {
                "total": len(recs),
                "by_sector": by_sector,
                "by_cap": cap_counts,
                "by_price": price_counts,  # v5.8 新增
            }

        # 最近30天历史汇总
        hist_rows = con.execute("""
            SELECT date,
                   SUM(CASE WHEN type='high_year' THEN 1 ELSE 0 END) as high_cnt,
                   SUM(CASE WHEN type='low_year' THEN 1 ELSE 0 END) as low_cnt
            FROM new_high_low_daily
            GROUP BY date
            ORDER BY date DESC
            LIMIT 30
        """).fetchall()

        # 每天Top3板块（从DB查）
        history = []
        for row in hist_rows:
            d = str(row[0])
            top_sectors_rows = con.execute("""
                SELECT sector, COUNT(*) as cnt
                FROM new_high_low_daily
                WHERE date = ? AND type = 'high_year' AND sector != '未知'
                GROUP BY sector
                ORDER BY cnt DESC
                LIMIT 3
            """, [d]).fetchall()
            top_sectors = [r[0] for r in top_sectors_rows]
            history.append({
                "date": d,
                "high_year_total": row[1],
                "low_year_total": row[2],
                "top_sectors": top_sectors,
            })

        con.close()

        # 写JSON
        output = clean_nan({
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today": today_data,
            "history": history,
        })

        json_path = os.path.join(DATA_DIR, "new_high_low.json")
        tmp_path = json_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)

        print(f"  ✅ 新高新低明细: {count}条存DuckDB + 导出new_high_low.json "
              f"(年新高{today_data.get('high_year',{}).get('total',0)}, "
              f"年新低{today_data.get('low_year',{}).get('total',0)})")

    except ImportError:
        print("  ⚠️ duckdb未安装，跳过新高新低明细")
    except Exception as e:
        print(f"  ⚠️ 新高新低明细保存失败: {e}")


def collect_ashare_overview():
    """主采集函数"""
    print("[ashare_overview] 开始采集...")

    # 1. 全市场行情KPI
    kpi = {}
    cap_distribution = {}
    market_distribution = []
    df = None
    try:
        df = load_spot_data()
        kpi = calc_kpi(df)
        print(f"  ✅ KPI: 上涨{kpi.get('up_count')}, 下跌{kpi.get('down_count')}, "
              f"涨停{kpi.get('limit_up')}, 跌停{kpi.get('limit_down')}")
    except Exception as e:
        print(f"  ❌ KPI: {e}")

    # 1b. 大中小微盘涨跌分布
    if df is not None:
        try:
            cap_distribution = calc_cap_distribution(df)
        except Exception as e:
            print(f"  ❌ 涨跌分布: {e}")
        try:
            market_distribution = calc_market_distribution(df)
        except Exception as e:
            print(f"  ❌ 全市场分布: {e}")

    time.sleep(0.5)

    # 2. 连续涨跌（同花顺，收盘后延迟刷新 → 15:10跳过，16:40补采）
    skip_ths = os.environ.get("HUIPAN_SKIP_THS") == "1"
    consec = {}
    if skip_ths:
        print("  ⏭️ 跳过同花顺统计（延迟采集）")
    else:
        consec = fetch_consecutive()
    kpi.update(consec)

    # 3. 创新高/新低（同花顺，同上）
    year_details = []
    if not skip_ths:
        try:
            hl_kpi, year_details = fetch_new_highs_lows()
            kpi.update(hl_kpi)
        except Exception as e:
            print(f"  ❌ 创新高新低: {e}")
            for k in ["high_month", "low_month", "high_year", "low_year", "high_ath", "low_ath"]:
                kpi[k] = 0

    time.sleep(0.5)

    # 4. 大中小微盘指数
    cap_indices = fetch_cap_indices()

    time.sleep(0.5)

    # 5. 板块热力图
    sectors, all_sectors = fetch_sectors()

    # 5b. 板块连涨/连跌streak（从DuckDB历史数据计算）
    if sectors:
        sector_names = [s["name"] for s in (all_sectors or sectors)]
        streaks = calc_sector_streaks(sector_names)
        if streaks:
            for s in sectors:
                s["streak"] = streaks.get(s["name"], 0)

    time.sleep(0.5)

    # 6. ETF
    etf_vol, etf_chg = fetch_etfs()

    # 组装（空数据回退：读取旧JSON保留上次有效值）
    old_data = {}
    fallback_path = LIVE_OUTPUT_PATH if os.path.exists(LIVE_OUTPUT_PATH) else OUTPUT_PATH
    if os.path.exists(fallback_path):
        try:
            with open(fallback_path, "r", encoding="utf-8") as f:
                old_data = json.load(f)
        except Exception:
            pass

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    # 连板存活：当日后续运行跳过计算时，保留首次结果
    if "lianban_survived" not in kpi and old_data.get("kpi", {}).get("lianban_survived") is not None:
        kpi["lianban_survived"] = old_data["kpi"]["lianban_survived"]
        kpi["lianban_total"] = old_data["kpi"]["lianban_total"]
    # 同花顺字段：跳过THS采集时，保留上次有效值
    if skip_ths:
        THS_KEYS = ["consecutive_up_3", "consecutive_down_3",
                     "high_month", "low_month", "high_year", "low_year", "high_ath", "low_ath"]
        old_kpi = old_data.get("kpi", {})
        for k in THS_KEYS:
            if k not in kpi and k in old_kpi:
                kpi[k] = old_kpi[k]
    data = {
        "date": date_str,
        "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "kpi": kpi,
        "cap_indices": cap_indices if cap_indices else old_data.get("cap_indices", {}),
        "cap_distribution": cap_distribution if cap_distribution else old_data.get("cap_distribution", {}),
        "market_distribution": market_distribution if market_distribution else old_data.get("market_distribution", []),
        "sectors": sectors if sectors else old_data.get("sectors", []),
        "etf_vol": etf_vol if etf_vol else old_data.get("etf_vol", []),
        "etf_chg": etf_chg if etf_chg else old_data.get("etf_chg", []),
    }

    if not sectors and old_data.get("sectors"):
        print("  ⚠️ 板块数据为空，保留上次有效数据")
    if not cap_distribution and old_data.get("cap_distribution"):
        print("  ⚠️ 涨跌分布为空，保留上次有效数据")
    if not market_distribution and old_data.get("market_distribution"):
        print("  ⚠️ 全市场分布为空，保留上次有效数据")

    data = clean_nan(data)

    # 盘中实时文件：每次都写
    with open(LIVE_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[ashare_overview] → {LIVE_OUTPUT_PATH}")

    # 收盘归档文件：15:00后写入（当日定稿）
    if now.hour >= 15:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[ashare_overview] → {OUTPUT_PATH} (收盘归档)")

    # 7. KPI快照存DuckDB（30日排名用）
    save_kpi_to_duckdb(kpi, date_str)

    # 8. 板块全量数据存DuckDB（v3.4新增）
    try:
        from storage.duckdb_v3_tables import DuckDBV3Store
        store = DuckDBV3Store(os.path.join(BASE_DIR, "data", "huipan.duckdb"))
        sectors_to_save = all_sectors if all_sectors else sectors
        if sectors_to_save:
            store.save_ashare_sectors(date_str, sectors_to_save)
    except Exception as e:
        print(f"  ⚠️ 板块DuckDB入库跳过: {e}")

    # 9. 新高新低个股明细存DuckDB + 导出JSON（v3.10新增）
    if year_details:
        save_new_high_low_to_duckdb(date_str, year_details)

    return data


def collect_delayed_stats():
    """延迟采集同花顺统计数据（连涨连跌 + 新高新低）
    设计：15:10收盘时这些数据源尚未刷新，推迟到16:40再跑。
    只更新 ashare_overview.json 中对应字段，不影响实时KPI。
    """
    print("[ashare_overview] 延迟统计采集（连涨连跌+新高新低）...")

    # 1. 连续涨跌
    consec = fetch_consecutive()

    # 2. 新高新低
    year_details = []
    hl_kpi = {}
    try:
        hl_kpi, year_details = fetch_new_highs_lows()
    except Exception as e:
        print(f"  ❌ 创新高新低: {e}")
        for k in ["high_month", "low_month", "high_year", "low_year", "high_ath", "low_ath"]:
            hl_kpi[k] = 0

    # 3. 合并到现有 JSON（优先读live，fallback读daily）
    source_path = LIVE_OUTPUT_PATH if os.path.exists(LIVE_OUTPUT_PATH) else OUTPUT_PATH
    if not os.path.exists(source_path):
        print(f"  ❌ overview文件不存在，无法合并")
        return

    with open(source_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    kpi = data.get("kpi", {})
    kpi.update(consec)
    kpi.update(hl_kpi)
    data["kpi"] = kpi
    data["delayed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data = clean_nan(data)
    # 写入两个文件（delayed_stats只在16:40跑，必定是收盘后）
    for path in [LIVE_OUTPUT_PATH, OUTPUT_PATH]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[ashare_overview] 延迟统计已合并 → {OUTPUT_PATH} + live")

    # 4. 新高新低明细存DuckDB
    if year_details:
        date_str = datetime.now().strftime("%Y-%m-%d")
        save_new_high_low_to_duckdb(date_str, year_details)

    return data


if __name__ == "__main__":
    if (len(sys.argv) > 1 and sys.argv[1] == "--delayed") or os.environ.get("HUIPAN_DELAYED") == "1":
        collect_delayed_stats()
    else:
        collect_ashare_overview()
