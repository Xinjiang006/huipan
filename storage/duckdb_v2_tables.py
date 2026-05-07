"""
慧盘 v2 · 数据库建表
storage/duckdb_v2_tables.py

运行一次即可：python3 duckdb_v2_tables.py
如果表已存在则跳过（CREATE TABLE IF NOT EXISTS）
"""

import duckdb
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "huipan.duckdb")


def create_v2_tables(db_path: str = None):
    if db_path is None:
        db_path = DB_PATH

    con = duckdb.connect(db_path)

    # ── 1. news_articles · 新闻存储 ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS news_articles (
            id              VARCHAR PRIMARY KEY,
            source          VARCHAR NOT NULL,
            url             VARCHAR,
            title_en        VARCHAR,
            title_zh        VARCHAR,
            summary_zh      VARCHAR,
            tags            VARCHAR,
            related_sectors VARCHAR,
            sentiment       VARCHAR,
            importance      INTEGER DEFAULT 0,
            tier            INTEGER DEFAULT 0,
            keywords        VARCHAR,
            published_at    TIMESTAMP,
            fetched_at      TIMESTAMP NOT NULL,
            processed       BOOLEAN DEFAULT FALSE
        )
    """)
    print("  ✅ news_articles")

    # ── 2. us_sector_daily · 美股板块ETF每日数据 ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS us_sector_daily (
            date            DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            name_en         VARCHAR,
            close           DOUBLE,
            change_pct      DOUBLE,
            PRIMARY KEY (date, symbol)
        )
    """)
    print("  ✅ us_sector_daily")

    # ── 3. us_stock_movers · 美股动量个股 ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS us_stock_movers (
            date            DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            price           DOUBLE,
            change          DOUBLE,
            change_pct      DOUBLE,
            mover_type      VARCHAR,
            fetched_at      TIMESTAMP,
            PRIMARY KEY (date, symbol, mover_type)
        )
    """)
    print("  ✅ us_stock_movers")

    # ── 4. global_market · 全球行情快照 ──
    con.execute("""
        CREATE TABLE IF NOT EXISTS global_market (
            date            DATE,
            symbol          VARCHAR,
            name            VARCHAR,
            category        VARCHAR,
            value           DOUBLE,
            change_pct      DOUBLE,
            PRIMARY KEY (date, symbol)
        )
    """)
    print("  ✅ global_market")

    HK_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS hk_stock_movers (
        date            DATE,
        code            VARCHAR,
        name            VARCHAR,
        price           DOUBLE,
        change          DOUBLE,
        change_pct      DOUBLE,
        volume_hkd      DOUBLE,
        mover_type      VARCHAR,
        fetched_at      TIMESTAMP,
        PRIMARY KEY (date, code, mover_type)
    )

    # 验证
    tables = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'main'
        ORDER BY table_name
    """).fetchall()

    print(f"\n  数据库全部表（{len(tables)}张）：")
    for (t,) in tables:
        count = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"    {t:25s} | {count} 行")

    con.close()
    print(f"\n  数据库路径: {os.path.abspath(db_path)}")


if __name__ == "__main__":
    print("=" * 50)
    print("慧盘 v2 · 建表")
    print("=" * 50)
    create_v2_tables()
    print("\n完成")
