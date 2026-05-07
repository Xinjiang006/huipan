#!/usr/bin/env python3
"""
migrate_regime_dates.py — regime_history.json 日期语义迁移 (v2)
用法: python3 migrate_regime_dates.py [--dry-run]

迁移逻辑:
  1. 删除 date==picks_date 的冗余记录（早期手动运行产生）
  2. 剩余记录: date不变, 补 return_date=date
  
语义: date=return_date=当天交易日(T+1), picks_date=选股日(T)
"""

import json
import shutil
import sys
from pathlib import Path

# 自动适配 static/data/ 或 data/ 目录
DATA_DIR = Path(__file__).parent / "static" / "data"
if not DATA_DIR.exists():
    DATA_DIR = Path(__file__).parent / "data"

REGIME_FILE = DATA_DIR / "regime_history.json"
DRY_RUN = "--dry-run" in sys.argv


def main():
    if not REGIME_FILE.exists():
        print(f"❌ 文件不存在: {REGIME_FILE}")
        return

    with open(REGIME_FILE, "r") as f:
        records = json.load(f)

    print(f"📄 读取 {len(records)} 条记录")
    print()

    # Phase 1: 标记冗余记录 (date==picks_date)
    remove_indices = []
    for i, r in enumerate(records):
        d = r.get("date", "")[:10]
        p = r.get("picks_date", "")[:10]
        if d == p:
            remove_indices.append(i)
            print(f"  🗑️  删除 Record[{i}]: date=picks={d}, fetched_at={r.get('fetched_at', '')[:19]}")

    # Phase 2: 过滤并补 return_date
    migrated = []
    for i, r in enumerate(records):
        if i in remove_indices:
            continue

        date_val = r["date"][:10]
        picks_val = r["picks_date"][:10]

        # date 不变, 补 return_date = date
        r["return_date"] = date_val + "T00:00:00.000"

        migrated.append(r)
        print(f"  ✅ Record[{i}]: date={date_val}(不变), picks={picks_val}, return_date={date_val}(新增)")

    print()
    print(f"📊 结果: {len(records)}条 → 删除{len(remove_indices)}条 → 保留{len(migrated)}条")

    # Phase 3: 验证
    errors = []
    dates = [r["date"][:10] for r in migrated]
    if len(dates) != len(set(dates)):
        errors.append("存在重复date")
    for i, r in enumerate(migrated):
        if r["picks_date"][:10] >= r["date"][:10]:
            errors.append(f"Record[{i}]: picks_date({r['picks_date'][:10]}) >= date({r['date'][:10]})")
        if r["return_date"][:10] != r["date"][:10]:
            errors.append(f"Record[{i}]: return_date ≠ date")

    if errors:
        print("❌ 验证失败:")
        for e in errors:
            print(f"  - {e}")
        return

    # Phase 4: 写入
    if DRY_RUN:
        print("\n🔍 DRY RUN 模式，不写入文件")
    else:
        backup = REGIME_FILE.with_suffix(".json.bak_migrate")
        shutil.copy2(REGIME_FILE, backup)
        print(f"\n💾 备份: {backup}")

        with open(REGIME_FILE, "w") as f:
            json.dump(migrated, f, ensure_ascii=False, indent=2)
        print(f"✅ 已写入 {REGIME_FILE}")


if __name__ == "__main__":
    main()
