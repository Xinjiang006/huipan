"""
慧盘 v2 · 数据导出模块
storage/data_export.py

每次采集入库后自动导出 JSON，同时支持手动导出。
两份输出：最新版（前端固定读）+ 带时间戳存档。

目录结构：
  static/data/
    news.json              ← 最新版
    us_sectors.json
    us_movers.json
    hk_movers.json
    global_market.json
    archive/
      news_20260311_0700.json
      us_sectors_20260311.json
      hk_movers_20260311.json
      ...
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

# 项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "static", "data")
_ARCHIVE_DIR = os.path.join(_DATA_DIR, "archive")


def _ensure_dirs():
    os.makedirs(_DATA_DIR, exist_ok=True)
    os.makedirs(_ARCHIVE_DIR, exist_ok=True)


def _write_json(data: dict | list, latest_name: str, archive_name: str, verbose: bool = True):
    """
    写两份文件：最新版覆盖 + 存档

    参数:
        data: 要写入的数据
        latest_name: 最新版文件名（如 "news.json"）
        archive_name: 存档文件名（如 "news_20260311_0700.json"）
    """
    _ensure_dirs()

    latest_path = os.path.join(_DATA_DIR, latest_name)
    archive_path = os.path.join(_ARCHIVE_DIR, archive_name)

    content = json.dumps(data, ensure_ascii=False, indent=2)

    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(content)

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(content)

    if verbose:
        size_kb = len(content.encode("utf-8")) / 1024
        print(f"  📄 {latest_name} ({size_kb:.1f}KB) + archive/{archive_name}")


def _now_str(include_time: bool = True) -> str:
    """返回北京时间的时间戳字符串"""
    from datetime import timedelta
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    if include_time:
        return now_bj.strftime("%Y%m%d_%H%M")
    return now_bj.strftime("%Y%m%d")


# ══════════════════════════════════════════════
#  1. 新闻导出
# ══════════════════════════════════════════════

def export_news(items: list[dict], verbose: bool = True) -> str:
    """
    导出新闻列表

    参数:
        items: collect_news() 返回值或从DB查询的新闻列表
            [{id, source, title_en, url, tier, keywords, fetched_at}, ...]

    返回:
        最新版文件路径
    """
    data = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "items": items,
    }

    ts = _now_str(include_time=True)  # 新闻一天多次，带时间
    _write_json(data, "news.json", f"news_{ts}.json", verbose=verbose)

    return os.path.join(_DATA_DIR, "news.json")

def export_news_from_db(db_path: Optional[str] = None, verbose: bool = True) -> str:
    """从数据库导出最近48小时新闻"""
    import duckdb
    if db_path is None:
        db_path = os.path.join(_PROJECT_ROOT, "data", "huipan.duckdb")
    con = duckdb.connect(db_path)
    rows = con.execute("""
        SELECT id, source, title_en, url, tier, keywords, fetched_at
        FROM news_articles
        WHERE fetched_at >= NOW() - INTERVAL 48 HOUR
        ORDER BY tier ASC, fetched_at DESC
    """).fetchall()
    con.close()
    items = [{
        "id": r[0], "source": r[1], "title_en": r[2], "url": r[3],
        "tier": r[4], "keywords": r[5], "fetched_at": str(r[6]),
    } for r in rows]
    return export_news(items, verbose=verbose)

# ══════════════════════════════════════════════
#  2. ETF 板块导出
# ══════════════════════════════════════════════

def export_sectors(etfs: list[dict], verbose: bool = True) -> str:
    """
    导出板块ETF数据

    参数:
        etfs: collect_sina_etfs() 返回值
            [{symbol, name, name_en, close, change_pct, date}, ...]
    """
    data = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(etfs),
        "items": etfs,
    }

    ts = _now_str(include_time=False)  # 日频，只带日期
    _write_json(data, "us_sectors.json", f"us_sectors_{ts}.json", verbose=verbose)

    return os.path.join(_DATA_DIR, "us_sectors.json")


# ══════════════════════════════════════════════
#  3. 美股动量榜导出
# ══════════════════════════════════════════════

def export_movers(movers: dict, trade_date: str = "", verbose: bool = True) -> str:
    """
    导出Yahoo动量榜

    参数:
        movers: collect_yahoo_movers() 返回值
            {gainers: [...], losers: [...], most_active: [...], trending: [...]}
        trade_date: 交易日期
    """
    data = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trade_date": trade_date,
        "gainers": movers.get("gainers", []),
        "losers": movers.get("losers", []),
        "most_active": movers.get("most_active", []),
        "trending": movers.get("trending", []),
        "total": sum(len(movers.get(k, [])) for k in ["gainers", "losers", "most_active", "trending"]),
    }

    ts = _now_str(include_time=False)
    _write_json(data, "us_movers.json", f"us_movers_{ts}.json", verbose=verbose)

    return os.path.join(_DATA_DIR, "us_movers.json")


# ══════════════════════════════════════════════
#  4. 全球行情导出
# ══════════════════════════════════════════════

def export_global(items: list[dict], verbose: bool = True) -> str:
    """
    导出全球行情

    参数:
        items: collect_global() 返回值
            [{name, category, value, change_pct}, ...]
    """
    # 按分组组织
    groups = {}
    for item in items:
        cat = item.get("category", "other")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(item)

    data = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "items": items,
        "by_category": groups,
    }

    ts = _now_str(include_time=False)
    _write_json(data, "global_market.json", f"global_market_{ts}.json", verbose=verbose)

    return os.path.join(_DATA_DIR, "global_market.json")


# ══════════════════════════════════════════════
#  5. 港股通动量榜导出
# ══════════════════════════════════════════════

def export_hk_movers(data_in: dict, verbose: bool = True) -> str:
    """
    导出港股通榜单

    参数:
        data_in: collect_hk_movers() 返回值
            {hk_gainers: [...], hk_losers: [...], hk_volume: [...], total_count: 590}
    """
    data = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_count": data_in.get("total_count", 0),
        "hk_gainers": data_in.get("hk_gainers", []),
        "hk_losers": data_in.get("hk_losers", []),
        "hk_volume": data_in.get("hk_volume", []),
    }

    ts = _now_str(include_time=False)
    _write_json(data, "hk_movers.json", f"hk_movers_{ts}.json", verbose=verbose)

    return os.path.join(_DATA_DIR, "hk_movers.json")


# ══════════════════════════════════════════════
#  手动导出：从数据库读取全部最新数据
# ══════════════════════════════════════════════

def export_all_from_db(db_path: Optional[str] = None, verbose: bool = True):
    """
    从 DuckDB 读取全部最新数据并导出。
    手动跑：python3 -m storage.data_export
    """
    import duckdb

    if db_path is None:
        db_path = os.path.join(_PROJECT_ROOT, "data", "huipan.duckdb")

    if verbose:
        print("\n📦 从数据库导出全部数据")
        print("-" * 40)

    con = duckdb.connect(db_path)

    # 1. 新闻
    rows = con.execute("""
        SELECT id, source, title_en, url, tier, keywords, fetched_at
        FROM news_articles
        ORDER BY tier ASC, fetched_at DESC
    """).fetchall()
    news_items = [{
        "id": r[0], "source": r[1], "title_en": r[2], "url": r[3],
        "tier": r[4], "keywords": r[5], "fetched_at": str(r[6]),
    } for r in rows]
    export_news(news_items, verbose=verbose)

    # 2. ETF（最新日期）
    rows = con.execute("""
        SELECT symbol, name, name_en, close, change_pct, date
        FROM us_sector_daily
        WHERE date = (SELECT MAX(date) FROM us_sector_daily)
        ORDER BY change_pct DESC
    """).fetchall()
    etf_items = [{
        "symbol": r[0], "name": r[1], "name_en": r[2],
        "close": r[3], "change_pct": r[4], "date": str(r[5]),
    } for r in rows]
    export_sectors(etf_items, verbose=verbose)

    # 3. 美股动量榜（最新日期）
    latest_date = con.execute("SELECT MAX(date) FROM us_stock_movers").fetchone()[0]
    movers = {}
    if latest_date:
        for mtype in ["gainers", "losers", "most_active", "trending"]:
            mrows = con.execute(f"""
                SELECT symbol, name, price, change, change_pct
                FROM us_stock_movers
                WHERE date = ? AND mover_type = ?
                ORDER BY ABS(change_pct) DESC
            """, [latest_date, mtype]).fetchall()
            movers[mtype] = [{
                "symbol": r[0], "name": r[1], "price": r[2],
                "change": r[3], "change_pct": r[4],
            } for r in mrows]
    export_movers(movers, trade_date=str(latest_date or ""), verbose=verbose)

    # 4. 全球行情（最新日期）
    rows = con.execute("""
        SELECT symbol, name, category, value, change_pct
        FROM global_market
        WHERE date = (SELECT MAX(date) FROM global_market)
        ORDER BY category, name
    """).fetchall()
    global_items = [{
        "symbol": r[0], "name": r[1], "category": r[2],
        "value": r[3], "change_pct": r[4],
    } for r in rows]
    export_global(global_items, verbose=verbose)

    # 5. 港股通榜单（最新日期）
    hk_tables = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_name = 'hk_stock_movers'
    """).fetchall()

    hk_count = 0
    if hk_tables:
        hk_latest = con.execute("SELECT MAX(date) FROM hk_stock_movers").fetchone()[0]
        hk_data = {"total_count": 0}
        if hk_latest:
            for mtype in ["hk_gainers", "hk_losers", "hk_volume"]:
                hk_rows = con.execute("""
                    SELECT code, name, price, change, change_pct, volume_hkd
                    FROM hk_stock_movers
                    WHERE date = ? AND mover_type = ?
                    ORDER BY ABS(change_pct) DESC
                """, [hk_latest, mtype]).fetchall()
                hk_data[mtype] = [{
                    "code": r[0], "name": r[1], "price": r[2],
                    "change": r[3], "change_pct": r[4], "volume_hkd": r[5],
                } for r in hk_rows]
            hk_data["total_count"] = sum(len(hk_data.get(k, [])) for k in ["hk_gainers", "hk_losers", "hk_volume"])
        export_hk_movers(hk_data, verbose=verbose)
        hk_count = hk_data["total_count"]

    con.close()

    if verbose:
        print(f"\n✅ 全部导出完成 → {_DATA_DIR}/")
        print(f"   新闻: {len(news_items)} 条")
        print(f"   ETF: {len(etf_items)} 只")
        print(f"   美股动量榜: {sum(len(v) for v in movers.values())} 只")
        print(f"   港股动量榜: {hk_count} 只")
        print(f"   全球行情: {len(global_items)} 项")


# ── CLI ──────────────────────────────────────

if __name__ == "__main__":
    export_all_from_db()
