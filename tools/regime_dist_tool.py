#!/usr/bin/env python3
"""
慧盘 · regime_daily 分布字段工具
三种模式：
  --dry-run  对比显示旧值 vs 新值（默认）
  --commit   实际UPDATE到DB（保留指数/T+1等其他字段不动）
  --test     回归测试：验证 calc 函数输出与DB当前值是否一致

用法：
  cd ~/huipan
  python3 tools/regime_dist_tool.py                # dry-run（默认）
  python3 tools/regime_dist_tool.py --commit        # 执行更新
  python3 tools/regime_dist_tool.py --test          # 回归测试
  python3 tools/regime_dist_tool.py --dates 2026-04-01 2026-04-02  # 只处理指定日期

设计思路：
  - 迁移用途：将历史record的分布字段从"picks(T-1)"改为"spot(当天)"
  - 测试用途：改了calc_sector_stats/calc_cap_dist等函数后，跑--test验证影响范围
  - 数据安全：只UPDATE分布相关的~20个字段，不碰指数/T+1/衍生/健康等

v4.8 · 2026-04-05
"""

import os
import sys
import json
import pickle
import argparse
from datetime import datetime

# ─── 项目根目录 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from compute.indicators import (
    calc_cap_dist,
    calc_price_dist,
    calc_sector_stats,
)
from data_io.regime_io import (
    ARCHIVE_SPOT_DIR,
    DB_PATH,
    HISTORY_PATH,
)
from collector.ashare_movers import (
    build_picks_from_df,
    load_sector_map,
    load_index_cons_map,
)

# ── 需要UPDATE/测试的分布字段 ──
DIST_FIELDS = [
    "sector_dist_gainers", "sector_dist_losers",
    "gainer_micro", "gainer_small", "gainer_mid", "gainer_large",
    "loser_micro", "loser_small", "loser_mid", "loser_large",
    "gainer_p0_10", "gainer_p10_30", "gainer_p30_50", "gainer_p50_100", "gainer_p100p",
    "loser_p0_10", "loser_p10_30", "loser_p30_50", "loser_p50_100", "loser_p100p",
    "sector_count_gainers", "sector_count_losers", "sector_overlap",
    "top_gainer_sectors", "top_loser_sectors",
    "micro_cap_ratio_gainer",
]

JSON_FIELDS = {"sector_dist_gainers", "sector_dist_losers",
               "top_gainer_sectors", "top_loser_sectors"}


# ══════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════

def list_archive_dates():
    """列出所有归档pkl日期（升序）"""
    if not os.path.isdir(ARCHIVE_SPOT_DIR):
        return []
    dates = []
    for f in os.listdir(ARCHIVE_SPOT_DIR):
        if f.startswith("spot_") and f.endswith(".pkl"):
            d = f[5:13]
            try:
                ds = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
                dates.append(ds)
            except Exception:
                continue
    dates.sort()
    return dates


def load_pkl(date_str):
    """加载指定日期的归档pkl"""
    fname = f"spot_{date_str.replace('-', '')}.pkl"
    path = os.path.join(ARCHIVE_SPOT_DIR, fname)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            cache = pickle.load(f)
        df = cache.get("df")
        return df if df is not None and len(df) > 0 else None
    except Exception as e:
        print(f"  ⚠️ pkl加载失败({fname}): {e}")
        return None


def compute_distribution(df, sector_map, cons_map):
    """从df计算全部分布字段，返回dict"""
    gainers, losers = build_picks_from_df(df, sector_map, cons_map)
    if gainers is None or not gainers:
        return None

    gainer_cap = calc_cap_dist(gainers)
    loser_cap = calc_cap_dist(losers)
    gainer_price = calc_price_dist(gainers)
    loser_price = calc_price_dist(losers)
    sector_stats = calc_sector_stats(gainers, losers)

    return {
        "gainer_micro": gainer_cap.get("微盘", 0),
        "gainer_small": gainer_cap.get("小盘", 0),
        "gainer_mid":   gainer_cap.get("中盘", 0),
        "gainer_large": gainer_cap.get("大盘", 0),
        "loser_micro":  loser_cap.get("微盘", 0),
        "loser_small":  loser_cap.get("小盘", 0),
        "loser_mid":    loser_cap.get("中盘", 0),
        "loser_large":  loser_cap.get("大盘", 0),
        "gainer_p0_10":   gainer_price.get("0-10", 0),
        "gainer_p10_30":  gainer_price.get("10-30", 0),
        "gainer_p30_50":  gainer_price.get("30-50", 0),
        "gainer_p50_100": gainer_price.get("50-100", 0),
        "gainer_p100p":   gainer_price.get("100+", 0),
        "loser_p0_10":    loser_price.get("0-10", 0),
        "loser_p10_30":   loser_price.get("10-30", 0),
        "loser_p30_50":   loser_price.get("30-50", 0),
        "loser_p50_100":  loser_price.get("50-100", 0),
        "loser_p100p":    loser_price.get("100+", 0),
        **sector_stats,
    }


def load_db_record(con, date_str):
    """从DB读取指定日期的record"""
    try:
        row = con.execute(
            "SELECT * FROM regime_daily WHERE date = ?", [date_str]
        ).fetchdf()
        if row.empty:
            return None
        record = json.loads(row.to_json(orient="records", date_format="iso"))[0]
        # DuckDB JSON列导出为字符串，还原为对象
        for k in JSON_FIELDS:
            v = record.get(k)
            if isinstance(v, str):
                try:
                    record[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        return record
    except Exception as e:
        print(f"  ⚠️ DB读取失败({date_str}): {e}")
        return None


def compare_fields(old, new, fields):
    """对比两组字段值，返回[(field, old_val, new_val), ...]"""
    diffs = []
    for f in fields:
        old_v = old.get(f) if old else None
        new_v = new.get(f)
        # JSON字段可能是字符串，先还原
        if f in JSON_FIELDS:
            if isinstance(old_v, str):
                try:
                    old_v = json.loads(old_v)
                except (json.JSONDecodeError, TypeError):
                    pass
        if old_v != new_v:
            diffs.append((f, old_v, new_v))
    return diffs


def validate_distribution(new):
    """数值完整性校验，返回问题列表"""
    issues = []

    # 涨幅市值分布加总 ≈ 100
    cap_sum = sum(new.get(f, 0) or 0
                  for f in ["gainer_micro", "gainer_small", "gainer_mid", "gainer_large"])
    if cap_sum < 85 or cap_sum > 105:
        issues.append(f"gainer市值加总={cap_sum}（期望≈100）")

    # 跌幅市值分布加总 ≈ 100
    lcap_sum = sum(new.get(f, 0) or 0
                   for f in ["loser_micro", "loser_small", "loser_mid", "loser_large"])
    if lcap_sum < 85 or lcap_sum > 105:
        issues.append(f"loser市值加总={lcap_sum}（期望≈100）")

    # sector_dist_gainers 加总 ≈ 100
    sd = new.get("sector_dist_gainers", {})
    if isinstance(sd, dict) and sd:
        sd_sum = sum(sd.values())
        if sd_sum < 85 or sd_sum > 105:
            issues.append(f"sector_dist_gainers加总={sd_sum}（期望≈100）")

    # sector_count 在合理范围
    sc = new.get("sector_count_gainers")
    if sc is not None and (sc < 3 or sc > 31):
        issues.append(f"sector_count_gainers={sc}（期望5~28）")

    return issues


def get_db_dates(con, filter_dates=None):
    """获取DB中的日期列表"""
    rows = con.execute(
        "SELECT date FROM regime_daily ORDER BY date"
    ).fetchall()
    all_dates = [r[0].strftime("%Y-%m-%d") for r in rows]
    if filter_dates:
        all_dates = [d for d in all_dates if d in set(filter_dates)]
    return all_dates


def re_export_history(con):
    """重新导出 regime_history.json"""
    rows = con.execute(
        "SELECT * FROM regime_daily ORDER BY date DESC LIMIT 30"
    ).fetchdf()
    history = json.loads(rows.to_json(orient="records", date_format="iso", force_ascii=False))
    for rec in history:
        for k in JSON_FIELDS:
            v = rec.get(k)
            if isinstance(v, str):
                try:
                    rec[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    return len(history)


# ══════════════════════════════════════════════
# 三种模式
# ══════════════════════════════════════════════

def run_dry_run(sector_map, cons_map, filter_dates=None):
    """--dry-run: 对比显示，不写DB"""
    import duckdb

    con = duckdb.connect(DB_PATH, read_only=True)
    db_dates = get_db_dates(con, filter_dates)
    archive_dates = set(list_archive_dates())

    print(f"📦 DB记录: {len(db_dates)}天, 归档pkl: {len(archive_dates)}天\n")

    matched = diffed = skipped = 0

    for date_str in db_dates:
        # 决定用哪个pkl
        pkl_date = date_str
        if pkl_date not in archive_dates:
            # 旧record: date=picks_date, 实际数据在return_date
            db_rec = load_db_record(con, date_str)
            rd = db_rec.get("return_date", "")
            if isinstance(rd, str) and rd[:10] in archive_dates:
                pkl_date = rd[:10]

        df = load_pkl(pkl_date)
        if df is None:
            print(f"  ⏭️  {date_str}: 无pkl({pkl_date})，跳过")
            skipped += 1
            continue

        old = load_db_record(con, date_str)
        new = compute_distribution(df, sector_map, cons_map)

        if new is None:
            print(f"  ⚠️  {date_str}: 无法从pkl提取Top100")
            skipped += 1
            continue

        # 校验
        issues = validate_distribution(new)

        # 对比
        diffs = compare_fields(old, new, DIST_FIELDS)

        if not diffs:
            print(f"  ✅ {date_str}: 完全一致")
            matched += 1
        else:
            print(f"  🔄 {date_str}: {len(diffs)}个字段有差异 (pkl={pkl_date})")
            for f, ov, nv in diffs[:6]:
                ov_s = _fmt(ov)
                nv_s = _fmt(nv)
                print(f"     {f}: {ov_s} → {nv_s}")
            if len(diffs) > 6:
                print(f"     ... 还有{len(diffs) - 6}个")
            diffed += 1

        if issues:
            for iss in issues:
                print(f"     ⚠️ 校验: {iss}")

    con.close()
    print(f"\n{'=' * 55}")
    print(f"汇总: ✅一致{matched} | 🔄有差异{diffed} | ⏭️跳过{skipped}")
    if diffed > 0:
        print(f"\n差异是预期的（旧=picks分布，新=当天spot分布）")
        print(f"确认无误后运行: python3 tools/regime_dist_tool.py --commit")


def run_commit(sector_map, cons_map, filter_dates=None):
    """--commit: 实际更新DB"""
    import duckdb

    con = duckdb.connect(DB_PATH)
    db_dates = get_db_dates(con, filter_dates)
    archive_dates = set(list_archive_dates())

    print(f"📦 准备更新 {len(db_dates)} 条记录...\n")

    updated = skipped = failed = 0

    for date_str in db_dates:
        pkl_date = date_str
        if pkl_date not in archive_dates:
            db_rec = load_db_record(con, date_str)
            rd = db_rec.get("return_date", "") if db_rec else ""
            if isinstance(rd, str) and rd[:10] in archive_dates:
                pkl_date = rd[:10]

        df = load_pkl(pkl_date)
        if df is None:
            skipped += 1
            continue

        new = compute_distribution(df, sector_map, cons_map)
        if new is None:
            skipped += 1
            continue

        issues = validate_distribution(new)
        if issues:
            print(f"  ⚠️  {date_str}: 校验不通过，跳过 ({'; '.join(issues)})")
            failed += 1
            continue

        # 构建UPDATE SQL
        sets = []
        values = []
        for f in DIST_FIELDS:
            v = new.get(f)
            if f in JSON_FIELDS:
                sets.append(f"{f} = json(?)")
                values.append(json.dumps(v, ensure_ascii=False) if v is not None else "{}")
            else:
                sets.append(f"{f} = ?")
                values.append(v)

        values.append(date_str)
        sql = f"UPDATE regime_daily SET {', '.join(sets)} WHERE date = ?"

        try:
            con.execute(sql, values)
            updated += 1
            print(f"  ✅ {date_str}: 已更新 (pkl={pkl_date})")
        except Exception as e:
            print(f"  ❌ {date_str}: UPDATE失败 — {e}")
            failed += 1

    # 重新导出JSON
    n = re_export_history(con)
    con.close()

    print(f"\n{'=' * 55}")
    print(f"完成: ✅更新{updated} | ⏭️跳过{skipped} | ❌失败{failed}")
    print(f"regime_history.json 已重新导出({n}天)")


def run_test(sector_map, cons_map, filter_dates=None):
    """--test: 回归测试 — 验证calc函数与DB值一致"""
    import duckdb

    con = duckdb.connect(DB_PATH, read_only=True)
    db_dates = get_db_dates(con, filter_dates)
    archive_dates = set(list_archive_dates())

    print(f"🧪 回归测试: {len(db_dates)} 条记录\n")

    passed = failed = skipped = 0
    failures = []

    for date_str in db_dates:
        # 测试模式：直接用date对应的pkl（测的是函数逻辑一致性）
        df = load_pkl(date_str)
        if df is None:
            skipped += 1
            continue

        db_rec = load_db_record(con, date_str)
        calc_rec = compute_distribution(df, sector_map, cons_map)

        if calc_rec is None:
            skipped += 1
            continue

        diffs = compare_fields(db_rec, calc_rec, DIST_FIELDS)

        if not diffs:
            passed += 1
        else:
            failed += 1
            failures.append((date_str, diffs))

    con.close()

    # 报告
    total = passed + failed
    print(f"\n{'=' * 55}")
    print(f"回归测试: {passed}/{total} PASSED  ({skipped} skipped)")

    if failures:
        print(f"\n❌ FAILED ({len(failures)} dates):")
        for date_str, diffs in failures:
            print(f"\n  📅 {date_str} — {len(diffs)} fields differ:")
            for f, db_v, calc_v in diffs[:8]:
                print(f"    {f}:")
                print(f"      DB:   {_fmt(db_v)}")
                print(f"      calc: {_fmt(calc_v)}")
            if len(diffs) > 8:
                print(f"    ... +{len(diffs) - 8} more")

        print(f"\n💡 如果是故意修改了计算逻辑，运行 --commit 更新DB")
        print(f"   如果不是，请检查最近的代码改动")
    else:
        print("\n✅ 所有分布字段与DB一致，计算函数无回归问题!")


# ══════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════

def _fmt(val, max_len=60):
    """格式化值的显示"""
    if val is None:
        return "None"
    if isinstance(val, dict):
        s = json.dumps(val, ensure_ascii=False)
        return s if len(s) <= max_len else s[:max_len - 3] + "..."
    if isinstance(val, list):
        s = json.dumps(val, ensure_ascii=False)
        return s if len(s) <= max_len else s[:max_len - 3] + "..."
    return str(val)


# ══════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="regime_daily 分布字段工具 — 迁移/对比/回归测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 tools/regime_dist_tool.py              # 默认dry-run，对比所有日期
  python3 tools/regime_dist_tool.py --commit     # 实际更新DB
  python3 tools/regime_dist_tool.py --test       # 回归测试
  python3 tools/regime_dist_tool.py --dates 2026-04-01 2026-04-02  # 只处理指定日期
        """,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="对比显示（默认）")
    mode.add_argument("--commit", action="store_true", help="实际UPDATE到DB")
    mode.add_argument("--test", action="store_true", help="回归测试")
    parser.add_argument("--dates", nargs="+", help="只处理指定日期（空格分隔）")

    args = parser.parse_args()

    # 加载映射
    print("=" * 55)
    print("慧盘 · regime_daily 分布字段工具 v4.8")
    print("=" * 55)

    sector_map = load_sector_map()
    cons_map = load_index_cons_map()
    sm_count = len(sector_map) if isinstance(sector_map, dict) else 0
    cm_count = sum(len(v) for v in cons_map.values()) if isinstance(cons_map, dict) and cons_map else 0
    print(f"📦 sector_map: {sm_count}只, cons_map: {cm_count}只\n")

    if args.test:
        run_test(sector_map, cons_map, args.dates)
    elif args.commit:
        run_commit(sector_map, cons_map, args.dates)
    else:
        run_dry_run(sector_map, cons_map, args.dates)


if __name__ == "__main__":
    main()
