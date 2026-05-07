"""
慧盘 · 板块映射表更新脚本
生成 config/sector_map.json：code → sector_name

三层策略（按优先级）：
  Layer 1: AKShare THS board接口动态获取（~90个板块，每周一运行）
  Layer 2: .spot_cache.pkl 中的行业字段（如果存在）
  Layer 3: 读不到时填"未知"，不阻塞主流程

用法：
  python3 collector/update_sector_map.py        # 正常更新
  python3 collector/update_sector_map.py --force # 强制覆盖（忽略更新时间检查）
"""

import json
import os
import sys
import pickle
import time
import argparse
from datetime import datetime

import akshare as ak

# ─── 路径 ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
OUTPUT_PATH = os.path.join(CONFIG_DIR, "sector_map.json")
CACHE_PATH = os.path.join(DATA_DIR, ".spot_cache.pkl")

os.makedirs(CONFIG_DIR, exist_ok=True)


def load_existing_map():
    """读取已有映射表（用于合并增量更新）"""
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("map", {}), data.get("updated_at", "")
        except Exception:
            pass
    return {}, ""


def layer1_from_ths_boards(existing_map):
    """
    Layer 1: 同花顺board接口动态获取
    ak.stock_board_industry_name_ths() → 板块列表
    ak.stock_board_industry_cons_ths(symbol=板块名) → 成分股
    """
    print("[sector_map] Layer1: THS板块接口获取...")
    sector_map = dict(existing_map)  # 从已有map开始，增量更新
    success_count = 0
    fail_count = 0

    try:
        boards_df = ak.stock_board_industry_name_ths()
        # 列名容错
        col_name = next((c for c in boards_df.columns if "板块" in c or "名称" in c or "name" in c.lower()), boards_df.columns[0])
        board_names = boards_df[col_name].tolist()
        print(f"  获取到{len(board_names)}个板块")
    except Exception as e:
        print(f"  ❌ 获取板块列表失败: {e}")
        return sector_map, 0

    for i, board_name in enumerate(board_names):
        try:
            cons_df = ak.stock_board_industry_cons_ths(symbol=board_name)
            col_code = next((c for c in cons_df.columns if "代码" in c or "code" in c.lower()), None)
            if col_code is None:
                continue
            for code in cons_df[col_code].tolist():
                # 统一格式：去前缀，保留6位数字
                code_clean = str(code).replace("sh", "").replace("sz", "").replace("bj", "").zfill(6)
                sector_map[code_clean] = board_name
            success_count += 1

            # 限速：每10个板块暂停1秒，避免THS限频
            if (i + 1) % 10 == 0:
                print(f"  已处理 {i+1}/{len(board_names)} 个板块...")
                time.sleep(1)

        except Exception as e:
            fail_count += 1
            if fail_count <= 3:  # 只打印前3个失败
                print(f"  ⚠️ 板块[{board_name}]获取失败: {e}")
            time.sleep(0.5)  # 失败后短暂等待

    print(f"  ✅ Layer1完成: {success_count}板块成功, {fail_count}失败, 共{len(sector_map)}只股票")
    return sector_map, success_count


def layer2_from_spot_cache(sector_map):
    """
    Layer 2: 从 .spot_cache.pkl 补充板块字段
    stock_zh_a_spot() 如果有"所属行业"或类似列就直接用
    """
    print("[sector_map] Layer2: 从spot_cache补充...")
    if not os.path.exists(CACHE_PATH):
        print("  ⚠️ spot_cache.pkl不存在，跳过Layer2")
        return sector_map, 0

    try:
        with open(CACHE_PATH, "rb") as f:
            cache = pickle.load(f)
        df = cache.get("df")
        if df is None:
            return sector_map, 0

        # 查找板块/行业列
        sector_col = next(
            (c for c in df.columns if any(kw in c for kw in ["行业", "板块", "sector", "industry"])),
            None
        )
        code_col = next((c for c in df.columns if "代码" in c), None)

        if sector_col is None or code_col is None:
            print(f"  ℹ️ spot_cache无板块字段（列：{list(df.columns[:8])}...），跳过Layer2")
            return sector_map, 0

        added = 0
        for _, row in df.iterrows():
            code = str(row[code_col]).replace("sh", "").replace("sz", "").replace("bj", "")
            sector = str(row[sector_col]).strip()
            if code not in sector_map and sector and sector != "nan":
                sector_map[code] = sector
                added += 1

        print(f"  ✅ Layer2补充{added}只股票的板块信息")
        return sector_map, added

    except Exception as e:
        print(f"  ⚠️ Layer2失败: {e}")
        return sector_map, 0


def save_map(sector_map, layer1_count, layer2_count):
    """原子写入（temp文件 + rename）"""
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_stocks": len(sector_map),
        "layer1_boards": layer1_count,
        "layer2_supplement": layer2_count,
        "map": sector_map
    }

    tmp_path = OUTPUT_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, OUTPUT_PATH)  # 原子操作
        print(f"[sector_map] ✅ 已保存 {len(sector_map)} 只股票的板块映射 → {OUTPUT_PATH}")
    except Exception as e:
        print(f"[sector_map] ❌ 保存失败: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def main(force=False):
    existing_map, updated_at = load_existing_map()

    if existing_map and not force:
        print(f"[sector_map] 已有映射表（{len(existing_map)}只，更新于{updated_at}），本次增量更新")
    else:
        print("[sector_map] 全量重建映射表")
        existing_map = {}

    # Layer 1: THS动态获取
    sector_map, l1_count = layer1_from_ths_boards(existing_map)

    # Layer 2: spot_cache补充
    sector_map, l2_count = layer2_from_spot_cache(sector_map)

    # 保存
    save_map(sector_map, l1_count, l2_count)
    return sector_map


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="强制全量重建")
    args = parser.parse_args()
    main(force=args.force)
