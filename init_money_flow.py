"""
北向资金历史补充
stock_hsgt_hist_em 分别拉沪股通/深股通，合并后写入 money_flow 表
只补 2022-01-01 起的数据
"""
import akshare as ak
import pandas as pd
from datetime import date
from loguru import logger
from storage.duckdb_store import upsert_many, init_tables

START_DATE = date(2022, 1, 1)

def run():
    init_tables()
    logger.info("拉取北向资金历史...")

    df_sh = ak.stock_hsgt_hist_em(symbol="沪股通")
    df_sz = ak.stock_hsgt_hist_em(symbol="深股通")

    df_sh["日期"] = pd.to_datetime(df_sh["日期"]).dt.date
    df_sz["日期"] = pd.to_datetime(df_sz["日期"]).dt.date

    df_sh = df_sh[df_sh["日期"] >= START_DATE].set_index("日期")
    df_sz = df_sz[df_sz["日期"] >= START_DATE].set_index("日期")

    rows = []
    for d in df_sh.index:
        sh_net = float(df_sh.loc[d, "当日成交净买额"])
        sz_net = float(df_sz.loc[d, "当日成交净买额"]) if d in df_sz.index else 0.0
        rows.append({
            "date":              d,
            "north_sh_net":      sh_net,
            "north_sz_net":      sz_net,
            "north_total_net":   round(sh_net + sz_net, 4),
            "main_net_inflow":   0.0,
            "retail_net_inflow": 0.0,
            "margin_balance":    0.0,
        })

    upsert_many("money_flow", rows)
    logger.info(f"north向资金写入 {len(rows)} 条，范围 {rows[0]['date']} → {rows[-1]['date']}")

if __name__ == "__main__":
    run()
