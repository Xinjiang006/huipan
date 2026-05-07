#!/usr/bin/env python3
"""
一次性修复脚本：rebuild --full 把指数/剪刀差/新高新低差/波动率等字段覆盖成null，
从旧 regime_history.json 恢复这些字段到 DuckDB，然后重新导出 JSON。

用法: cd ~/huipan && python3 restore_rebuild_nulls.py
"""
import duckdb
import json
import os

DB_PATH = "data/huipan.duckdb"
JSON_PATH = "data/regime_history.json"

# 从本次session上传的旧regime_history.json提取的值
RESTORE_DATA = {
    "2026-04-03": {"sh_change_pct": -1.0, "sz_change_pct": -0.99, "cyb_change_pct": -0.73, "csi1000_change_pct": -1.3, "cap_scissors": 0.3, "new_high_low_diff": -44, "volatility_5d": 0.955, "volume_rank_30d": 0, "breadth_5d_avg": 50.68},
    "2026-04-02": {"sh_change_pct": -0.74, "sz_change_pct": -1.6, "cyb_change_pct": -2.31, "csi1000_change_pct": -1.68, "cap_scissors": 0.94, "new_high_low_diff": 22, "volatility_5d": 1.046, "volume_rank_30d": 0, "breadth_5d_avg": 50.02},
    "2026-04-01": {"sh_change_pct": 1.46, "sz_change_pct": 1.7, "cyb_change_pct": 1.96, "csi1000_change_pct": 1.69, "cap_scissors": -0.23, "new_high_low_diff": -431, "volatility_5d": 1.062, "volume_rank_30d": 0, "breadth_5d_avg": 50.82},
    "2026-03-31": {"sh_change_pct": -0.8, "sz_change_pct": -1.81, "cyb_change_pct": -2.7, "csi1000_change_pct": -1.39, "cap_scissors": 0.59, "new_high_low_diff": -45, "volatility_5d": 0.902, "volume_rank_30d": 0, "breadth_5d_avg": 55.22},
    "2026-03-30": {"sh_change_pct": 0.24, "sz_change_pct": -0.25, "cyb_change_pct": -0.68, "csi1000_change_pct": 0.12, "cap_scissors": 0.12, "new_high_low_diff": 83, "volatility_5d": 1.12, "volume_rank_30d": 0, "breadth_5d_avg": 64.34},
    "2026-03-27": {"sh_change_pct": 0.63, "sz_change_pct": 1.13, "cyb_change_pct": 0.71, "csi1000_change_pct": 1.66, "cap_scissors": -1.03, "new_high_low_diff": -189, "volatility_5d": 2.213, "volume_rank_30d": 0, "breadth_5d_avg": 56.04},
    "2026-03-26": {"sh_change_pct": -1.09, "sz_change_pct": -1.41, "cyb_change_pct": -1.34, "csi1000_change_pct": -1.34, "cap_scissors": 0.25, "new_high_low_diff": 139, "volatility_5d": 2.476, "volume_rank_30d": 0, "breadth_5d_avg": 47.06},
    "2026-03-25": {"sh_change_pct": 1.3, "sz_change_pct": 1.95, "cyb_change_pct": 2.01, "csi1000_change_pct": 1.99, "cap_scissors": -0.69, "new_high_low_diff": -1, "volatility_5d": 2.963, "volume_rank_30d": 0, "breadth_5d_avg": 42.3},
    "2026-03-24": {"sh_change_pct": 1.78, "sz_change_pct": 1.43, "cyb_change_pct": 0.5, "csi1000_change_pct": 2.59, "cap_scissors": -0.81, "new_high_low_diff": -1, "volatility_5d": 3.825, "volume_rank_30d": 0, "breadth_5d_avg": 30.6},
    "2026-03-23": {"sh_change_pct": -3.63, "sz_change_pct": -3.76, "cyb_change_pct": -3.49, "csi1000_change_pct": -3.46, "cap_scissors": -0.17, "new_high_low_diff": -1353, "volume_rank_30d": 0, "breadth_5d_avg": 10.97},
    "2026-03-20": {"csi1000_change_pct": -1.74, "new_high_low_diff": -1340, "volume_rank_30d": 1, "breadth_5d_avg": 18.5},
    "2026-03-19": {"csi1000_change_pct": -1.74, "new_high_low_diff": -1340, "volume_rank_30d": 0},
}

FIELDS = ["sh_change_pct", "sz_change_pct", "cyb_change_pct", "csi1000_change_pct",
          "cap_scissors", "new_high_low_diff", "volatility_5d", "volume_rank_30d", "breadth_5d_avg"]


def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ 找不到 {DB_PATH}")
        return

    conn = duckdb.connect(DB_PATH)

    updated = 0
    for date_str, vals in RESTORE_DATA.items():
        set_parts = []
        params = []
        for f in FIELDS:
            if f in vals:
                set_parts.append(f"{f} = ?")
                params.append(vals[f])
        if not set_parts:
            continue
        params.append(date_str)
        sql = f"UPDATE regime_daily SET {', '.join(set_parts)} WHERE date = ?"
        conn.execute(sql, params)
        updated += 1
        print(f"  ✅ {date_str}: 恢复 {len(set_parts)} 个字段")

    print(f"\n共更新 {updated} 天")

    # 重新导出 regime_history.json
    rows = conn.execute("SELECT * FROM regime_daily ORDER BY date DESC").fetchall()
    cols = [d[0] for d in conn.description]
    records = []
    for row in rows:
        rec = {}
        for c, v in zip(cols, row):
            if hasattr(v, 'isoformat'):
                rec[c] = v.isoformat()
            else:
                rec[c] = v
        records.append(rec)

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"✅ {JSON_PATH} 已重新导出 ({len(records)} 条)")

    conn.close()


if __name__ == "__main__":
    main()
