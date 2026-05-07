"""
慧盘 v2 · 数据入库
storage/duckdb_v2_store.py

将采集器数据写入 DuckDB，追加到 storage/duckdb_store.py 或独立使用均可。

表：
  - news_articles     ← collector/news.py
  - us_stock_movers   ← collector/us_market.py (Yahoo榜单)
  - us_sector_daily   ← collector/us_market.py (Sina ETF)

依赖：duckdb
"""

import os
import duckdb
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "huipan.duckdb")


def _get_conn(db_path: Optional[str] = None):
    return duckdb.connect(db_path or DB_PATH)


# ══════════════════════════════════════════════
#  1. 新闻入库 · INSERT OR IGNORE
# ══════════════════════════════════════════════

def save_news(items: list[dict], db_path: Optional[str] = None, verbose: bool = True) -> int:
    """
    将 collect_news() 返回的新闻列表写入 news_articles 表。

    参数:
        items: collect_news() 的返回值
            [{id, source, title_en, url, tier, keywords: list, fetched_at}, ...]
        db_path: 数据库路径（默认项目标准路径）
        verbose: 是否打印日志

    返回:
        实际新增条数

    策略: INSERT OR IGNORE
        - 同一ID（md5(source:url)）的新闻跳过，保留首次采集时间
        - 保护已被 Deepseek 处理过的记录（title_zh/tags 等不会被覆盖）
    """
    if not items:
        if verbose:
            print("  ⚠️ save_news: 空列表，跳过")
        return 0

    con = _get_conn(db_path)

    # 入库前计数
    before = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]

    for item in items:
        # keywords: list → 逗号分隔字符串
        keywords_str = ",".join(item.get("keywords", []))

        # fetched_at: ISO字符串 → 保持字符串，DuckDB自动转TIMESTAMP
        fetched_at = item.get("fetched_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        con.execute("""
            INSERT OR IGNORE INTO news_articles
                (id, source, url, title_en, tier, keywords, fetched_at, processed)
            VALUES (?, ?, ?, ?, ?, ?, ?, FALSE)
        """, [
            item["id"],
            item["source"],
            item.get("url", ""),
            item["title_en"],
            item.get("tier", 0),
            keywords_str,
            fetched_at,
        ])

    # 入库后计数
    after = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    inserted = after - before

    con.close()

    if verbose:
        print(f"  💾 news_articles: {len(items)} 条提交 → {inserted} 条新增（{len(items) - inserted} 条已存在跳过）")
    from storage.data_export import export_news
    export_news(items, verbose=verbose)
    return inserted


# ══════════════════════════════════════════════
#  2. Yahoo 榜单入库 · INSERT OR REPLACE
# ══════════════════════════════════════════════

def save_movers(movers: dict, trade_date: str, db_path: Optional[str] = None, verbose: bool = True) -> int:
    """
    将 collect_yahoo_movers() 返回的榜单数据写入 us_stock_movers 表。

    参数:
        movers: collect_yahoo_movers() 的返回值
            {gainers: [{symbol, name, price, change, change_pct}], losers: [...], ...}
        trade_date: 美股交易日（字符串 "2026-03-07"）
            建议用 Sina ETF 返回的 date 字段，最可靠
        db_path: 数据库路径
        verbose: 是否打印日志

    返回:
        写入总条数

    策略: INSERT OR REPLACE
        - 主键 (date, symbol, mover_type)
        - 同一天同一只股票同一榜单覆盖更新
    """
    if not movers:
        if verbose:
            print("  ⚠️ save_movers: 空数据，跳过")
        return 0

    con = _get_conn(db_path)
    total = 0
    fetched_at = movers.get("fetched_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    for mover_type in ["gainers", "losers", "most_active", "trending"]:
        items = movers.get(mover_type, [])
        if not items:
            continue

        for item in items:
            con.execute("""
                INSERT OR REPLACE INTO us_stock_movers
                    (date, symbol, name, price, change, change_pct, mover_type, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                trade_date,
                item["symbol"],
                item.get("name", ""),
                item.get("price", 0.0),
                item.get("change", 0.0),
                item.get("change_pct", 0.0),
                mover_type,
                fetched_at,
            ])
            total += 1

        if verbose:
            print(f"  💾 us_stock_movers [{mover_type}]: {len(items)} 条")

    con.close()

    if verbose:
        print(f"  💾 us_stock_movers 合计: {total} 条写入（date={trade_date}）")
    from storage.data_export import export_movers
    export_movers(movers, trade_date=trade_date, verbose=verbose)
    return total


# ══════════════════════════════════════════════
#  3. Sina 板块 ETF 入库 · INSERT OR REPLACE
# ══════════════════════════════════════════════

def save_sector_daily(etfs: list[dict], db_path: Optional[str] = None, verbose: bool = True) -> int:
    """
    将 collect_sina_etfs() 返回的 ETF 数据写入 us_sector_daily 表。

    参数:
        etfs: collect_sina_etfs() 的返回值
            [{symbol, name, name_en, close, change_pct, date}, ...]
        db_path: 数据库路径
        verbose: 是否打印日志

    返回:
        写入条数

    策略: INSERT OR REPLACE
        - 主键 (date, symbol)
        - date 直接取 Sina 返回的日期（美股交易日）
    """
    if not etfs:
        if verbose:
            print("  ⚠️ save_sector_daily: 空列表，跳过")
        return 0

    con = _get_conn(db_path)
    count = 0

    for etf in etfs:
        etf_date = etf.get("date", "")
        if not etf_date:
            if verbose:
                print(f"  ⚠️ {etf.get('symbol', '?')} 无日期，跳过")
            continue

        con.execute("""
            INSERT OR REPLACE INTO us_sector_daily
                (date, symbol, name, name_en, close, change_pct)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            etf_date,
            etf["symbol"],
            etf.get("name", ""),
            etf.get("name_en", ""),
            etf.get("close", 0.0),
            etf.get("change_pct", 0.0),
        ])
        count += 1

    con.close()

    if verbose:
        print(f"  💾 us_sector_daily: {count} 条写入")
    from storage.data_export import export_sectors
    export_sectors(etfs, verbose=verbose)
    return count


# ══════════════════════════════════════════════
#  辅助：获取 Sina ETF 的 trade_date 供 movers 使用
# ══════════════════════════════════════════════

def get_trade_date_from_etfs(etfs: list[dict]) -> Optional[str]:
    """
    从 Sina ETF 数据中提取交易日期。
    用于给 Yahoo 榜单数据提供可靠的 trade_date。

    逻辑：取所有 ETF 返回的 date 中出现次数最多的值（防个别ETF延迟）
    """
    dates = [e.get("date", "") for e in etfs if e.get("date")]
    if not dates:
        return None

    from collections import Counter
    most_common = Counter(dates).most_common(1)[0][0]
    return most_common


# ══════════════════════════════════════════════
#  统一入口：采集 + 入库一步完成
# ══════════════════════════════════════════════

def collect_and_save_news(db_path: Optional[str] = None, verbose: bool = True) -> int:
    """采集新闻并入库，返回新增条数"""
    from collector.news import collect_news

    if verbose:
        print("\n📰 新闻采集 + 入库")
        print("-" * 40)

    items = collect_news(filter_keywords=True, verbose=verbose)
    return save_news(items, db_path=db_path, verbose=verbose)


def collect_and_save_us_market(db_path: Optional[str] = None, verbose: bool = True) -> dict:
    """
    采集美股数据并入库，返回写入统计。

    流程：
        1. 先采集 Sina ETF（拿到可靠的 trade_date）
        2. 再采集 Yahoo 榜单
        3. 两者用同一个 trade_date 入库
    """
    from collector.us_market import collect_sina_etfs, collect_yahoo_movers

    if verbose:
        print("\n🇺🇸 美股数据采集 + 入库")
        print("-" * 40)

    # Step 1: Sina ETF（先采集，拿 trade_date）
    if verbose:
        print("\n[1/2] Sina 板块 ETF")
    etfs = collect_sina_etfs(verbose=verbose)
    etf_count = save_sector_daily(etfs, db_path=db_path, verbose=verbose)

    # 从 ETF 数据提取交易日期
    trade_date = get_trade_date_from_etfs(etfs)
    if not trade_date:
        if verbose:
            print("  ❌ 无法从 Sina ETF 获取交易日期，Yahoo 榜单跳过")
        return {"etf_count": etf_count, "mover_count": 0, "trade_date": None}

    if verbose:
        print(f"  📅 交易日期: {trade_date}")

    # Step 2: Yahoo 榜单
    if verbose:
        print("\n[2/2] Yahoo Finance 榜单")
    movers = collect_yahoo_movers(verbose=verbose)
    mover_count = save_movers(movers, trade_date=trade_date, db_path=db_path, verbose=verbose)

    return {
        "etf_count": etf_count,
        "mover_count": mover_count,
        "trade_date": trade_date,
    }


# ── CLI 测试 ──────────────────────────────────

# ══════════════════════════════════════════════

def save_global(items: list[dict], trade_date: str = None, db_path: str = None, verbose: bool = True) -> int:
    """
    将 collect_global() 返回的全球行情写入 global_market 表。

    参数:
        items: collect_global() 的返回值
            [{name, category, value, change_pct}, ...]
        trade_date: 日期字符串（默认 today）
        db_path: 数据库路径
        verbose: 是否打印日志

    返回:
        写入条数

    策略: INSERT OR REPLACE
        - 主键 (date, symbol)
        - symbol 用 name 字段（中文名作为唯一标识）
    """
    if not items:
        if verbose:
            print("  ⚠️ save_global: 空列表，跳过")
        return 0

    from datetime import date as _date
    if not trade_date:
        trade_date = str(_date.today())

    con = _get_conn(db_path)
    count = 0

    for item in items:
        con.execute("""
            INSERT OR REPLACE INTO global_market
                (date, symbol, name, category, value, change_pct)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            trade_date,
            item["name"],       # 用中文名做 symbol（唯一性够用）
            item["name"],
            item.get("category", ""),
            item.get("value", 0.0),
            item.get("change_pct", 0.0),
        ])
        count += 1

    con.close()

    if verbose:
        print(f"  💾 global_market: {count} 条写入（date={trade_date}）")
    from storage.data_export import export_global
    export_global(items, verbose=verbose)
    return count

def save_hk_movers(data: dict, trade_date: str = None, db_path: str = None, verbose: bool = True) -> int:
    """
    将 collect_hk_movers() 返回的港股通榜单写入 hk_stock_movers 表。

    策略: INSERT OR REPLACE
    """
    if not data:
        if verbose:
            print("  ⚠️ save_hk_movers: 空数据，跳过")
        return 0

    from datetime import date as _date
    if not trade_date:
        trade_date = str(_date.today())

    con = _get_conn(db_path)
    total = 0
    fetched_at = data.get("fetched_at", "")

    for mover_type in ["hk_gainers", "hk_losers", "hk_volume"]:
        items = data.get(mover_type, [])
        for item in items:
            con.execute("""
                INSERT OR REPLACE INTO hk_stock_movers
                    (date, code, name, price, change, change_pct, volume_hkd, mover_type, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                trade_date,
                item["code"],
                item["name"],
                item.get("price", 0.0),
                item.get("change", 0.0),
                item.get("change_pct", 0.0),
                item.get("volume_hkd", 0.0),
                mover_type,
                fetched_at,
            ])
            total += 1

        if verbose:
            print(f"  💾 hk_stock_movers [{mover_type}]: {len(items)} 条")

    # 自动导出JSON
    from storage.data_export import export_hk_movers
    export_hk_movers(data, verbose=verbose)

    con.close()

    if verbose:
        print(f"  💾 hk_stock_movers 合计: {total} 条写入（date={trade_date}）")

    return total


def collect_and_save_hk(db_path: str = None, verbose: bool = True) -> int:
    """采集港股通榜单并入库"""
    from collector.hk_market import collect_hk_movers

    if verbose:
        print("\n🇭🇰 港股数据采集 + 入库")
        print("-" * 40)

    data = collect_hk_movers(verbose=verbose)
    return save_hk_movers(data, db_path=db_path, verbose=verbose)


"""
=== 4. storage/data_export.py · 加港股导出函数 ===

在 export_global() 后面加：
"""


def export_hk_movers(data: dict, verbose: bool = True) -> str:
    """
    导出港股通榜单

    参数:
        data: collect_hk_movers() 返回值
    """
    export_data = {
        "exported_at": None,  # 用 datetime 填
        "total_count": data.get("total_count", 0),
        "hk_gainers": data.get("hk_gainers", []),
        "hk_losers": data.get("hk_losers", []),
        "hk_volume": data.get("hk_volume", []),
    }

    from datetime import datetime, timezone
    export_data["exported_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ts = _now_str(include_time=False)
    _write_json(export_data, "hk_movers.json", f"hk_movers_{ts}.json", verbose=verbose)

    return os.path.join(_DATA_DIR, "hk_movers.json")
def collect_and_save_global(db_path: str = None, verbose: bool = True) -> int:
    """采集全球行情并入库，返回写入条数"""
    from collector.global_market import collect_global

    if verbose:
        print("\n🌍 全球行情采集 + 入库")
        print("-" * 40)

    items = collect_global(verbose=verbose)
    return save_global(items, db_path=db_path, verbose=verbose)
# ══════════════════════════════════════════════
#  5. 港股通榜单入库 · INSERT OR REPLACE
# ══════════════════════════════════════════════

def save_hk_movers(data: dict, trade_date: str = None, db_path: str = None, verbose: bool = True) -> int:
    if not data:
        if verbose:
            print("  ⚠️ save_hk_movers: 空数据，跳过")
        return 0

    from datetime import date as _date
    if not trade_date:
        trade_date = str(_date.today())

    con = _get_conn(db_path)
    total = 0
    fetched_at = data.get("fetched_at", "")

    for mover_type in ["hk_gainers", "hk_losers", "hk_volume"]:
        items = data.get(mover_type, [])
        for item in items:
            con.execute("""
                INSERT OR REPLACE INTO hk_stock_movers
                    (date, code, name, price, change, change_pct, volume_hkd, mover_type, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                trade_date,
                item["code"],
                item["name"],
                item.get("price", 0.0),
                item.get("change", 0.0),
                item.get("change_pct", 0.0),
                item.get("volume_hkd", 0.0),
                mover_type,
                fetched_at,
            ])
            total += 1

        if verbose:
            print(f"  💾 hk_stock_movers [{mover_type}]: {len(items)} 条")

    from storage.data_export import export_hk_movers
    export_hk_movers(data, verbose=verbose)

    con.close()

    if verbose:
        print(f"  💾 hk_stock_movers 合计: {total} 条写入（date={trade_date}）")

    return total


def collect_and_save_hk(db_path: str = None, verbose: bool = True) -> int:
    from collector.hk_market import collect_hk_movers

    if verbose:
        print("\n🇭🇰 港股数据采集 + 入库")
        print("-" * 40)

    data = collect_hk_movers(verbose=verbose)
    return save_hk_movers(data, db_path=db_path, verbose=verbose)
if __name__ == "__main__":
    print("=" * 60)
    print("慧盘 v2 · 入库测试")
    print("=" * 60)

    # 测试模式：用临时数据库
    import tempfile
    test_db = os.path.join(tempfile.gettempdir(), "huipan_test.duckdb")
    print(f"测试数据库: {test_db}")

    # 先建表
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from duckdb_v2_tables import create_v2_tables
    create_v2_tables(test_db)

    # 模拟新闻数据
    mock_news = [
        {
            "id": "test00000001",
            "source": "SCMP",
            "title_en": "China considers expanding fiscal stimulus targeting 4% deficit ratio",
            "url": "https://www.scmp.com/test1",
            "tier": 1,
            "keywords": ["china", "fiscal"],
            "fetched_at": "2026-03-10T07:00:00Z",
        },
        {
            "id": "test00000002",
            "source": "WSJ",
            "title_en": "US prepares expanded semiconductor export controls on China",
            "url": "https://www.wsj.com/test2",
            "tier": 1,
            "keywords": ["semiconductor", "china"],
            "fetched_at": "2026-03-10T07:00:00Z",
        },
    ]

    # 模拟 Yahoo 榜单
    mock_movers = {
        "gainers": [
            {"symbol": "NVDA", "name": "NVIDIA Corp", "price": 138.29, "change": 6.66, "change_pct": 5.06},
            {"symbol": "AAPL", "name": "Apple Inc", "price": 228.50, "change": 3.20, "change_pct": 1.42},
        ],
        "losers": [
            {"symbol": "TSLA", "name": "Tesla Inc", "price": 245.10, "change": -12.30, "change_pct": -4.78},
        ],
        "most_active": [],
        "trending": [],
        "fetched_at": "2026-03-10T06:00:00Z",
    }

    # 模拟 Sina ETF
    mock_etfs = [
        {"symbol": "XLK", "name": "科技", "name_en": "Technology", "close": 220.50, "change_pct": 1.23, "date": "2026-03-07"},
        {"symbol": "SOXX", "name": "半导体", "name_en": "Semiconductors", "close": 195.80, "change_pct": 2.85, "date": "2026-03-07"},
        {"symbol": "XLE", "name": "能源", "name_en": "Energy", "close": 88.40, "change_pct": -0.72, "date": "2026-03-07"},
    ]

    print("\n--- 测试 1: 新闻入库 ---")
    n1 = save_news(mock_news, db_path=test_db)
    print(f"  首次: 新增 {n1} 条")

    # 重复插入测试
    n2 = save_news(mock_news, db_path=test_db)
    print(f"  重复: 新增 {n2} 条（应为0）")

    print("\n--- 测试 2: ETF 入库 ---")
    e1 = save_sector_daily(mock_etfs, db_path=test_db)

    print("\n--- 测试 3: Yahoo 榜单入库 ---")
    trade_date = get_trade_date_from_etfs(mock_etfs)
    print(f"  trade_date = {trade_date}")
    m1 = save_movers(mock_movers, trade_date=trade_date, db_path=test_db)

    # 验证
    print("\n--- 验证 ---")
    con = duckdb.connect(test_db)
    for table in ["news_articles", "us_sector_daily", "us_stock_movers"]:
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} 行")

        # 打印前3行
        rows = con.execute(f"SELECT * FROM {table} LIMIT 3").fetchall()
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        for row in rows:
            brief = {cols[i]: row[i] for i in range(min(4, len(cols)))}
            print(f"    {brief}")

    con.close()

    # 清理
    os.remove(test_db)
    print(f"\n✅ 测试完成，临时数据库已清理")
