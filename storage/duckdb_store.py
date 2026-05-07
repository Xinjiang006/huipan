"""
DuckDB 行情数据存储层
"""
import duckdb
from pathlib import Path
from loguru import logger
from config import DUCKDB_PATH, DUCKDB_MEMORY_LIMIT


def get_conn() -> duckdb.DuckDBPyConnection:
    """获取 DuckDB 连接"""
    Path(DUCKDB_PATH).parent.mkdir(exist_ok=True)
    conn = duckdb.connect(str(DUCKDB_PATH))
    conn.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    return conn


def init_tables():
    """初始化所有数据表"""
    conn = get_conn()
    try:
        # 交易日历
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_calendar (
                date DATE PRIMARY KEY,
                is_trading BOOLEAN NOT NULL,
                prev_trading_date DATE,
                next_trading_date DATE
            )
        """)

        # 大盘情绪
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_sentiment (
                date DATE PRIMARY KEY,
                limit_up_count INTEGER,
                limit_down_count INTEGER,
                up_count INTEGER,
                down_count INTEGER,
                flat_count INTEGER,
                total_volume DOUBLE,
                sentiment_score DOUBLE
            )
        """)

        # 涨停记录
        conn.execute("""
            CREATE TABLE IF NOT EXISTS limit_up_records (
                date DATE,
                stock_code VARCHAR(6),
                stock_name VARCHAR,
                consecutive_days INTEGER,
                is_broken BOOLEAN,
                open_pct DOUBLE,
                sector VARCHAR,
                limit_up_time VARCHAR,
                PRIMARY KEY (date, stock_code)
            )
        """)

        # 板块资金流向
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_flow (
                date DATE,
                sector_name VARCHAR,
                change_pct DOUBLE,
                main_net_inflow DOUBLE,
                total_volume DOUBLE,
                up_count INTEGER,
                down_count INTEGER,
                PRIMARY KEY (date, sector_name)
            )
        """)

        # 资金流向
        conn.execute("""
            CREATE TABLE IF NOT EXISTS money_flow (
                date DATE PRIMARY KEY,
                north_sh_net DOUBLE,
                north_sz_net DOUBLE,
                north_total_net DOUBLE,
                main_net_inflow DOUBLE,
                retail_net_inflow DOUBLE,
                margin_balance DOUBLE
            )
        """)

        # ETF快照
        conn.execute("""
            CREATE TABLE IF NOT EXISTS etf_snapshot (
                date DATE,
                fund_code VARCHAR,
                fund_name VARCHAR,
                change_pct DOUBLE,
                share_change DOUBLE,
                total_share DOUBLE,
                total_size DOUBLE,
                category VARCHAR,
                PRIMARY KEY (date, fund_code)
            )
        """)

        # 北向资金每日
        conn.execute("""
            CREATE TABLE IF NOT EXISTS northbound_daily (
                date DATE PRIMARY KEY,
                net_inflow DOUBLE,
                buy_amount DOUBLE,
                sell_amount DOUBLE,
                consecutive_days INTEGER
            )
        """)

        conn.commit()
        logger.info("DuckDB 所有表初始化完成")
    finally:
        conn.close()


def upsert(table: str, data: dict, pk: list):
    """通用 upsert（insert or replace）"""
    conn = get_conn()
    try:
        cols = list(data.keys())
        placeholders = ", ".join(["?" for _ in cols])
        col_names = ", ".join(cols)

        # DuckDB 用 INSERT OR REPLACE
        sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
        conn.execute(sql, list(data.values()))
        conn.commit()
    finally:
        conn.close()


def upsert_many(table: str, rows: list[dict]):
    """批量 upsert"""
    if not rows:
        return
    conn = get_conn()
    try:
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?" for _ in cols])
        col_names = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
        values = [list(row.values()) for row in rows]
        conn.executemany(sql, values)
        conn.commit()
        logger.debug(f"{table} 写入 {len(rows)} 条")
    finally:
        conn.close()


def query(sql: str, params: list = None):
    """执行查询，返回 DataFrame"""
    conn = get_conn()
    try:
        if params:
            return conn.execute(sql, params).df()
        return conn.execute(sql).df()
    finally:
        conn.close()


if __name__ == "__main__":
    init_tables()
    print("✓ 数据库表初始化完成")


def save_llm_output(date, output_type: str, content: str, model: str = "rules"):
    """保存 LLM 生成的文本输出"""
    from loguru import logger
    logger.debug(f"LLM输出: [{output_type}] model={model} len={len(content)}")
