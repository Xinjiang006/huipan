"""
慧盘 v3.4 · DuckDB 新增5张表
──────────────────────────────
1. ashare_movers_daily   — A股涨跌榜+成交额榜（按日覆盖）
2. ashare_sector_daily   — 板块涨跌数据（按日覆盖）
3. commodity_daily       — 大宗商品每日快照（按日覆盖）
4. hot_rank_daily        — 雪球人气榜（按日覆盖）
5. investing_news        — 英为财经中文新闻（按文章去重）

用法：
    from storage.duckdb_v3_tables import DuckDBV3Store
    store = DuckDBV3Store('data/huipan.duckdb')
    store.init_tables()
    store.save_ashare_movers(date_str, movers_data)
    ...
"""

import duckdb
from datetime import datetime, date
from loguru import logger
import hashlib


class DuckDBV3Store:

    def __init__(self, db_path: str = 'data/huipan.duckdb'):
        self.db_path = db_path

    def _conn(self):
        return duckdb.connect(self.db_path)

    # ──────────────────────────────────────
    # 建表
    # ──────────────────────────────────────
    def init_tables(self):
        """创建5张新表（IF NOT EXISTS）"""
        con = self._conn()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS ashare_movers_daily (
                    date        DATE NOT NULL,
                    list_type   VARCHAR NOT NULL,   -- 'gainer' / 'loser' / 'volume'
                    rank        INTEGER NOT NULL,
                    code        VARCHAR,
                    name        VARCHAR,
                    price       DOUBLE,
                    change_pct  DOUBLE,
                    amount      DOUBLE,             -- 成交额（亿），volume榜用
                    fetched_at  TIMESTAMP,
                    PRIMARY KEY (date, list_type, rank)
                )
            """)

            con.execute("""
                CREATE TABLE IF NOT EXISTS ashare_sector_daily (
                    date        DATE NOT NULL,
                    name        VARCHAR NOT NULL,
                    change_pct  DOUBLE,
                    net_inflow  DOUBLE,             -- 主力净流入（亿）
                    fetched_at  TIMESTAMP,
                    PRIMARY KEY (date, name)
                )
            """)

            con.execute("""
                CREATE TABLE IF NOT EXISTS commodity_daily (
                    date        DATE NOT NULL,
                    category    VARCHAR,             -- 能源/金属/工业/指数
                    name        VARCHAR NOT NULL,
                    unit        VARCHAR,
                    price       DOUBLE,
                    day_change  DOUBLE,
                    day_pct     DOUBLE,
                    week_pct    DOUBLE,
                    month_pct   DOUBLE,
                    ytd_pct     DOUBLE,
                    yoy_pct     DOUBLE,
                    fetched_at  TIMESTAMP,
                    PRIMARY KEY (date, category, name)
                )
            """)

            con.execute("""
                CREATE TABLE IF NOT EXISTS hot_rank_daily (
                    date        DATE NOT NULL,
                    board       VARCHAR NOT NULL,    -- 'follow_hot'/'follow_new'/'tweet_hot'/...
                    rank        INTEGER NOT NULL,
                    code        VARCHAR,
                    name        VARCHAR,
                    price       DOUBLE,
                    value       DOUBLE,             -- 关注数/讨论数/交易量
                    fetched_at  TIMESTAMP,
                    PRIMARY KEY (date, board, rank)
                )
            """)

            con.execute("""
                CREATE TABLE IF NOT EXISTS investing_news (
                    id          VARCHAR PRIMARY KEY, -- md5(url)
                    title       VARCHAR,
                    url         VARCHAR,
                    category    VARCHAR,             -- 股市/宏观/期货
                    summary     VARCHAR,
                    published   TIMESTAMP,
                    fetched_at  TIMESTAMP
                )
            """)

            # v3.10: 新高新低个股明细（年度）
            con.execute("""
                CREATE TABLE IF NOT EXISTS new_high_low_daily (
                    date        DATE NOT NULL,
                    code        VARCHAR NOT NULL,
                    name        VARCHAR,
                    type        VARCHAR NOT NULL,   -- 'high_year' / 'low_year'
                    change_pct  DOUBLE,
                    price       DOUBLE,
                    cap_label   VARCHAR,             -- 大/中/小/微盘
                    sector      VARCHAR,             -- 申万一级行业
                    PRIMARY KEY (date, code, type)
                )
            """)

            logger.info("✅ DuckDB v3 tables initialized (6 tables)")
        finally:
            con.close()

    # ──────────────────────────────────────
    # 1. A股涨跌榜
    # ──────────────────────────────────────
    def save_ashare_movers(self, date_str: str, data: dict):
        """
        存储A股涨跌榜
        data 格式: { "gainers": [...], "losers": [...], "volume": [...] }
        每个item: { "code", "name", "price", "change_pct", "amount"(可选) }
        """
        con = self._conn()
        try:
            d = date_str  # "2026-03-15"
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            count = 0

            for list_type, key in [('gainer', 'gainers'), ('loser', 'losers'), ('volume', 'volume')]:
                items = data.get(key, [])
                if not items:
                    continue
                # 先删除当天该类型的旧数据
                con.execute(
                    "DELETE FROM ashare_movers_daily WHERE date = ? AND list_type = ?",
                    [d, list_type]
                )
                for i, item in enumerate(items):
                    con.execute("""
                        INSERT INTO ashare_movers_daily
                        (date, list_type, rank, code, name, price, change_pct, amount, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [
                        d, list_type, i + 1,
                        item.get('code', ''),
                        item.get('name', ''),
                        item.get('price'),
                        item.get('change_pct'),
                        item.get('amount'),
                        now,
                    ])
                    count += 1

            logger.info(f"✅ ashare_movers_daily: {count} rows saved for {d}")
        except Exception as e:
            logger.error(f"❌ ashare_movers_daily save failed: {e}")
        finally:
            con.close()

    # ──────────────────────────────────────
    # 2. 板块涨跌
    # ──────────────────────────────────────
    def save_ashare_sectors(self, date_str: str, sectors: list):
        """
        存储板块涨跌数据
        sectors: [{ "name", "change_pct", "net_inflow"(可选) }, ...]
        """
        con = self._conn()
        try:
            d = date_str
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 先删除当天旧数据
            con.execute("DELETE FROM ashare_sector_daily WHERE date = ?", [d])

            count = 0
            for s in sectors:
                con.execute("""
                    INSERT INTO ashare_sector_daily
                    (date, name, change_pct, net_inflow, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                """, [
                    d,
                    s.get('name', ''),
                    s.get('change_pct'),
                    s.get('net_inflow'),
                    now,
                ])
                count += 1

            logger.info(f"✅ ashare_sector_daily: {count} sectors saved for {d}")
        except Exception as e:
            logger.error(f"❌ ashare_sector_daily save failed: {e}")
        finally:
            con.close()

    # ──────────────────────────────────────
    # 3. 大宗商品
    # ──────────────────────────────────────
    def save_commodities(self, date_str: str, sections: list):
        """
        存储大宗商品每日快照
        sections: [{ "name":"能源", "items": [{ "name","unit","price","day_change",... }] }, ...]
        """
        con = self._conn()
        try:
            d = date_str
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 先删除当天旧数据
            con.execute("DELETE FROM commodity_daily WHERE date = ?", [d])

            count = 0
            for section in sections:
                category = section.get('name', '')
                for item in section.get('items', []):
                    con.execute("""
                        INSERT OR REPLACE INTO commodity_daily
                        (date, category, name, unit, price, day_change,
                         day_pct, week_pct, month_pct, ytd_pct, yoy_pct, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [
                        d, category,
                        item.get('name', ''),
                        item.get('unit', ''),
                        item.get('price'),
                        item.get('day_change'),
                        item.get('day_pct'),
                        item.get('week_pct'),
                        item.get('month_pct'),
                        item.get('ytd_pct'),
                        item.get('yoy_pct'),
                        now,
                    ])
                    count += 1

            logger.info(f"✅ commodity_daily: {count} items saved for {d}")
        except Exception as e:
            logger.error(f"❌ commodity_daily save failed: {e}")
        finally:
            con.close()

    # ──────────────────────────────────────
    # 4. 雪球人气榜
    # ──────────────────────────────────────
    def save_hot_rank(self, date_str: str, data: dict):
        """
        存储雪球人气榜
        data 格式: { "follow_hot": [...], "follow_new": [...], "tweet_hot": [...], ... }
        每个item: { "code", "name", "price", "value" }
        """
        con = self._conn()
        try:
            d = date_str
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 先删除当天旧数据
            con.execute("DELETE FROM hot_rank_daily WHERE date = ?", [d])

            count = 0
            for board, items in data.items():
                if not isinstance(items, list):
                    continue  # 跳过 fetched_at 等非列表字段
                for i, item in enumerate(items):
                    con.execute("""
                        INSERT INTO hot_rank_daily
                        (date, board, rank, code, name, price, value, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, [
                        d, board, i + 1,
                        item.get('code', ''),
                        item.get('name', ''),
                        item.get('price'),
                        item.get('value'),
                        now,
                    ])
                    count += 1

            logger.info(f"✅ hot_rank_daily: {count} rows saved for {d}")
        except Exception as e:
            logger.error(f"❌ hot_rank_daily save failed: {e}")
        finally:
            con.close()

    # ──────────────────────────────────────
    # 5. 英为财经中文新闻
    # ──────────────────────────────────────
    def save_investing_news(self, articles: list):
        """
        存储英为财经新闻（去重）
        articles: [{ "title", "url", "category", "summary", "published" }, ...]
        """
        con = self._conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            inserted = 0

            for a in articles:
                url = a.get('url', '')
                if not url:
                    continue
                news_id = hashlib.md5(url.encode()).hexdigest()[:12]

                try:
                    con.execute("""
                        INSERT OR IGNORE INTO investing_news
                        (id, title, url, category, summary, published, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, [
                        news_id,
                        a.get('title', ''),
                        url,
                        a.get('category', ''),
                        a.get('summary', ''),
                        a.get('published'),
                        now,
                    ])
                    # DuckDB INSERT OR IGNORE doesn't have rowcount,
                    # so we count all attempts
                    inserted += 1
                except Exception:
                    pass  # 重复跳过

            logger.info(f"✅ investing_news: {inserted} articles processed")
        except Exception as e:
            logger.error(f"❌ investing_news save failed: {e}")
        finally:
            con.close()
