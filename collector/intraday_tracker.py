"""
慧盘 · 盘中追踪模块
v3.6 新建

职责：
  1. 读 yesterday_picks.json（追涨组 + 抄底组）
  2. 读 opening_picks.json（开盘涨幅组 + 开盘跌幅组）
  3. 读 .spot_cache.pkl（当前盘中行情）
  4. 计算四组当前平均涨幅
  5. 追加到当天的 intraday_snapshot.json
  6. 写入 DuckDB intraday_tracking 表

四组人群：
  - 追涨组（momentum）：昨日收盘涨幅Top100今日表现
  - 抄底组（reversion）：昨日收盘跌幅Top100今日表现
  - 开盘涨幅组（open_bull）：今日竞价高开Top100当日表现
  - 开盘跌幅组（open_bear）：今日竞价低开Top100当日表现

调用方式：
  - 被 scheduler/jobs.py import：from collector.intraday_tracker import track_intraday
  - 独立运行：python3 collector/intraday_tracker.py

依赖：
  - static/data/.spot_cache.pkl
  - static/data/yesterday_picks.json
  - static/data/opening_picks.json
  - data/huipan.duckdb
"""

import json
import os
import math
import time
import pickle
import statistics
import requests
from datetime import datetime
from sources.index import fetch_indices as _fetch_raw_indices

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
CACHE_PATH = os.path.join(DATA_DIR, ".spot_cache.pkl")
PICKS_PATH = os.path.join(DATA_DIR, "yesterday_picks.json")
OPENING_PATH = os.path.join(DATA_DIR, "opening_picks.json")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "intraday_snapshot.json")
HISTORY_PATH = os.path.join(DATA_DIR, "intraday_history.json")
DB_PATH = os.path.join(BASE_DIR, "data", "huipan.duckdb")

# ─── DuckDB建表 ───
DDL_INTRADAY = """
CREATE TABLE IF NOT EXISTS intraday_tracking (
    date DATE,
    time_slot VARCHAR,

    -- 追涨组（昨日涨幅Top100今日表现）
    mom_avg DOUBLE,
    mom_median DOUBLE,
    mom_up_count INTEGER,
    mom_matched INTEGER,

    -- 抄底组（昨日跌幅Top100今日表现）
    rev_avg DOUBLE,
    rev_median DOUBLE,
    rev_up_count INTEGER,
    rev_matched INTEGER,

    -- 开盘涨幅组（今日竞价高开Top100当日表现）
    open_bull_avg DOUBLE,
    open_bull_median DOUBLE,
    open_bull_up_count INTEGER,
    open_bull_matched INTEGER,
    open_bull_intraday_avg DOUBLE,
    open_bull_intraday_median DOUBLE,
    open_bull_intraday_up_count INTEGER,

    -- 开盘跌幅组（今日竞价低开Top100当日表现）
    open_bear_avg DOUBLE,
    open_bear_median DOUBLE,
    open_bear_up_count INTEGER,
    open_bear_matched INTEGER,
    open_bear_intraday_avg DOUBLE,
    open_bear_intraday_median DOUBLE,
    open_bear_intraday_up_count INTEGER,

    -- 追涨/抄底组日内表现
    mom_intraday_avg DOUBLE,
    mom_intraday_up_count INTEGER,
    rev_intraday_avg DOUBLE,
    rev_intraday_up_count INTEGER,

    -- 三大指数（baseline参考）
    sh_change_pct DOUBLE,
    sz_change_pct DOUBLE,
    cyb_change_pct DOUBLE,

    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, time_slot)
)
"""


# ══════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════

def _load_json(path, label):
    """通用JSON加载"""
    if not os.path.exists(path):
        print(f"  ⚠️ {label}不存在（{os.path.basename(path)}），该组跳过")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ❌ {label}加载失败: {e}")
        return None


def _load_spot_chg_map():
    """
    从pkl缓存读取全市场 code → 当前涨跌幅 映射
    同时返回 intraday_map：code → (当前价-今开)/今开*100（日内涨跌，排除缺口）
    返回 (chg_map, intraday_map, cache_time) 或 (None, None, None)
    """
    try:
        with open(CACHE_PATH, "rb") as f:
            cache = pickle.load(f)
        df = cache.get("df")
        if df is None or len(df) == 0:
            print(f"  ❌ pkl为空")
            return None, None, None

        col_code  = next((c for c in df.columns if "代码" in c), None)
        col_chg   = next((c for c in df.columns if "涨跌幅" in c), None)
        col_price = next((c for c in df.columns if "最新价" in c), None)
        col_open  = next((c for c in df.columns if "今开" in c), None)

        if not col_code or not col_chg:
            print(f"  ❌ pkl缺少必要列")
            return None, None, None

        valid = df[df[col_price] > 0].copy() if col_price else df.copy()
        valid["_code"] = valid[col_code].str.replace(r"^[a-z]{2}", "", regex=True)

        chg_map = {}
        intraday_map = {}
        for _, row in valid.iterrows():
            code = row["_code"]
            try:
                chg = float(row[col_chg])
                if not math.isnan(chg):
                    chg_map[code] = chg
            except Exception:
                continue
            # 日内涨跌：(当前价 - 今开) / 今开 * 100
            if col_open:
                try:
                    price = float(row[col_price])
                    open_ = float(row[col_open])
                    if open_ > 0 and price > 0:
                        intraday_map[code] = round((price - open_) / open_ * 100, 3)
                except Exception:
                    pass

        age = time.time() - cache.get("time", 0)
        print(f"  ✅ pkl已加载（{len(chg_map)}只，{age:.0f}s前，日内{len(intraday_map)}只）")
        return chg_map, intraday_map, cache.get("time", 0)

    except Exception as e:
        print(f"  ❌ pkl读取失败: {e}")
        return None, None, None


def _calc_group_stats(picks_list, chg_map, intraday_map=None):
    """计算一组stock列表的当前涨跌统计 + 个股明细
    intraday_map: code → (当前价-今开)/今开*100，可选
    """
    if not picks_list:
        return {"avg": None, "median": None, "up_count": 0, "matched": 0,
                "intraday_avg": None, "intraday_median": None, "intraday_up_count": 0,
                "stocks": []}

    returns = []
    intraday_returns = []
    stocks = []
    for stock in picks_list:
        code = stock.get("code", "")
        chg = chg_map.get(code)
        if chg is not None:
            returns.append(chg)
            intra = intraday_map.get(code) if intraday_map else None
            if intra is not None:
                intraday_returns.append(intra)
            stocks.append({
                "code": code,
                "name": stock.get("name", ""),
                "orig_chg": stock.get("change_pct"),
                "now_chg": round(chg, 2),
                "intraday_chg": round(intra, 2) if intra is not None else None,
                "cap_label": stock.get("cap_label", ""),
                "sector": stock.get("sector", ""),
            })

    if not returns:
        return {"avg": None, "median": None, "up_count": 0, "matched": 0,
                "intraday_avg": None, "intraday_median": None, "intraday_up_count": 0,
                "stocks": []}

    stocks.sort(key=lambda s: s["now_chg"], reverse=True)

    return {
        "avg": round(sum(returns) / len(returns), 3),
        "median": round(statistics.median(returns), 3),
        "up_count": sum(1 for r in returns if r > 0),
        "matched": len(returns),
        # 日内表现（排除缺口，反映真实买入体验）
        "intraday_avg": round(sum(intraday_returns) / len(intraday_returns), 3) if intraday_returns else None,
        "intraday_median": round(statistics.median(intraday_returns), 3) if intraday_returns else None,
        "intraday_up_count": sum(1 for r in intraday_returns if r > 0) if intraday_returns else 0,
        "stocks": stocks,
    }


def _fetch_indices():
    """获取指数涨跌幅（通过Source层）"""
    KEY_MAP = {"sh000001": "sh", "sz399001": "sz", "sz399006": "cyb"}
    raw = _fetch_raw_indices()
    return {KEY_MAP[k]: v["change_pct"] for k, v in raw.items() if k in KEY_MAP}



# ══════════════════════════════════════════════
# 2. 主追踪函数
# ══════════════════════════════════════════════

def track_intraday():
    """
    主入口：计算四组当前表现，追加到snapshot JSON + DuckDB
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    time_slot = now.strftime("%H:%M")

    # 15:30之后不再追踪（防止收盘后--run all污染数据）
    if time_slot > "15:30":
        print(f"[intraday_tracker] {time_slot} 已过盘中时段，跳过")
        return None

    print(f"[intraday_tracker] {today_str} {time_slot} 开始追踪...")

    # 1. 加载行情
    chg_map, intraday_map, cache_time = _load_spot_chg_map()
    if chg_map is None:
        print("[intraday_tracker] 无行情数据，退出")
        return None

    # 2. 加载四组picks
    yesterday = _load_json(PICKS_PATH, "yesterday_picks")
    opening = _load_json(OPENING_PATH, "opening_picks")

    mom_list = yesterday.get("top100_gainers", []) if yesterday else []
    rev_list = yesterday.get("top100_losers", []) if yesterday else []
    open_bull_list = opening.get("top100_gainers", []) if opening else []
    open_bear_list = opening.get("top100_losers", []) if opening else []

    picks_date = yesterday.get("date", "") if yesterday else ""
    opening_date = opening.get("date", "") if opening else ""

    print(f"  追涨{len(mom_list)}只 抄底{len(rev_list)}只 "
          f"开盘涨{len(open_bull_list)}只 开盘跌{len(open_bear_list)}只")

    # 3. 计算四组（传入 intraday_map 计算日内表现）
    mom_stats       = _calc_group_stats(mom_list,       chg_map, intraday_map)
    rev_stats       = _calc_group_stats(rev_list,       chg_map, intraday_map)
    open_bull_stats = _calc_group_stats(open_bull_list, chg_map, intraday_map)
    open_bear_stats = _calc_group_stats(open_bear_list, chg_map, intraday_map)

    # 4. 指数baseline
    indices = _fetch_indices()

    snapshot = {
        "time": time_slot,
        "momentum": mom_stats,
        "reversion": rev_stats,
        "open_bull": open_bull_stats,
        "open_bear": open_bear_stats,
        "indices": {
            "sh": indices.get("sh"),
            "sz": indices.get("sz"),
            "cyb": indices.get("cyb"),
        },
    }

    print(f"  ✅ 追涨avg={mom_stats['avg']}% 抄底avg={rev_stats['avg']}% "
          f"开盘涨avg={open_bull_stats['avg']}% 开盘跌avg={open_bear_stats['avg']}%")

    # 5. 追加到intraday_snapshot.json（当天累积）
    _append_snapshot(today_str, picks_date, opening_date, snapshot)

    # 6. 写DuckDB
    _save_to_duckdb(today_str, time_slot, snapshot)

    # 7. 收盘后自动归档到历史（≥15:00触发）
    if time_slot >= "15:00":
        _save_intraday_history(today_str)

    return snapshot


def _append_snapshot(today_str, picks_date, opening_date, snapshot):
    """追加snapshot到当天JSON文件"""
    try:
        if os.path.exists(SNAPSHOT_PATH):
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 如果是昨天的文件，清空重建
            if data.get("date") != today_str:
                data = None
            # 如果picks_date变了（yesterday_picks被覆盖），拒绝追加
            elif picks_date and data.get("picks_date") and data["picks_date"] != picks_date:
                print(f"  ⚠️ picks_date已变（{data['picks_date']}→{picks_date}），跳过追加防止数据污染")
                return
        else:
            data = None

        if data is None:
            data = {
                "date": today_str,
                "picks_date": picks_date,
                "opening_date": opening_date,
                "snapshots": [],
            }

        # 如果同一个time_slot已经存在，覆盖（INSERT OR REPLACE语义）
        existing_times = {s["time"] for s in data["snapshots"]}
        if snapshot["time"] in existing_times:
            data["snapshots"] = [s for s in data["snapshots"] if s["time"] != snapshot["time"]]

        data["snapshots"].append(snapshot)

        # 按时间排序
        data["snapshots"].sort(key=lambda s: s["time"])

        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  ✅ intraday_snapshot.json已更新（{len(data['snapshots'])}个时间点）")

    except Exception as e:
        print(f"  ❌ snapshot写入失败: {e}")


def _save_to_duckdb(today_str, time_slot, snapshot):
    """写入DuckDB intraday_tracking表"""
    try:
        import duckdb
    except ImportError:
        print("  ⚠️ duckdb未安装，跳过")
        return

    if not os.path.exists(DB_PATH):
        print(f"  ⚠️ DuckDB不存在，跳过")
        return

    try:
        con = duckdb.connect(DB_PATH)
        con.execute(DDL_INTRADAY)

        # 迁移：旧表不含 intraday 列时自动添加
        for col, typ in [
            ("open_bull_intraday_avg", "DOUBLE"),
            ("open_bull_intraday_median", "DOUBLE"),
            ("open_bull_intraday_up_count", "INTEGER"),
            ("open_bear_intraday_avg", "DOUBLE"),
            ("open_bear_intraday_median", "DOUBLE"),
            ("open_bear_intraday_up_count", "INTEGER"),
            ("mom_intraday_avg", "DOUBLE"),
            ("mom_intraday_up_count", "INTEGER"),
            ("rev_intraday_avg", "DOUBLE"),
            ("rev_intraday_up_count", "INTEGER"),
        ]:
            try:
                con.execute(f"ALTER TABLE intraday_tracking ADD COLUMN {col} {typ}")
            except Exception:
                pass  # 列已存在，忽略

        mom = snapshot["momentum"]
        rev = snapshot["reversion"]
        ob  = snapshot["open_bull"]
        obr = snapshot["open_bear"]
        idx = snapshot.get("indices", {})

        con.execute("""
            INSERT OR REPLACE INTO intraday_tracking VALUES (
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                CURRENT_TIMESTAMP
            )
        """, [
            today_str, time_slot,
            mom["avg"], mom["median"], mom["up_count"], mom["matched"],
            rev["avg"], rev["median"], rev["up_count"], rev["matched"],
            ob["avg"],  ob["median"],  ob["up_count"],  ob["matched"],
            ob["intraday_avg"], ob["intraday_median"], ob["intraday_up_count"],
            obr["avg"], obr["median"], obr["up_count"], obr["matched"],
            obr["intraday_avg"], obr["intraday_median"], obr["intraday_up_count"],
            mom["intraday_avg"], mom["intraday_up_count"],
            rev["intraday_avg"], rev["intraday_up_count"],
            idx.get("sh"), idx.get("sz"), idx.get("cyb"),
        ])
        con.close()
        print(f"  ✅ intraday_tracking已写入DuckDB")

    except Exception as e:
        print(f"  ❌ DuckDB写入失败: {e}")
        import traceback
        traceback.print_exc()


def _save_intraday_history(today_str):
    """将当天收盘snapshot归档到intraday_history.json，保留最近5天"""
    try:
        # 读当天snapshot
        if not os.path.exists(SNAPSHOT_PATH):
            print("  ⚠️ 无snapshot，跳过历史归档")
            return
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            today_data = json.load(f)

        if today_data.get("date") != today_str:
            print("  ⚠️ snapshot日期不匹配，跳过历史归档")
            return

        snapshots = today_data.get("snapshots", [])
        if not snapshots:
            print("  ⚠️ snapshot无数据，跳过历史归档")
            return

        # 取最后一个时间点作为收盘快照
        last = snapshots[-1]
        record = {
            "date": today_str,
            "time": last.get("time"),
            "momentum": last.get("momentum"),
            "reversion": last.get("reversion"),
            "open_bull": last.get("open_bull"),
            "open_bear": last.get("open_bear"),
            "indices": last.get("indices"),
        }

        # 读已有历史
        history = []
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []

        # 去重：同一天只保留最新
        history = [h for h in history if h.get("date") != today_str]
        history.append(record)

        # 按日期降序，保留最近5天
        history.sort(key=lambda h: h.get("date", ""), reverse=True)
        history = history[:5]

        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

        print(f"  ✅ intraday_history.json已更新（{len(history)}天）")

    except Exception as e:
        print(f"  ❌ 历史归档失败: {e}")


# ══════════════════════════════════════════════
# 3. CLI入口
# ══════════════════════════════════════════════

if __name__ == "__main__":
    track_intraday()
