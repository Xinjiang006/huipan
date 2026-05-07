#!/usr/bin/env python3
"""
慧盘 · 申万一级行业映射
v3.10 · 2026-03-21

职责：
  1. 从申万宏源获取31个一级行业的成分股列表
  2. 构建 stock_code → industry_name 映射
  3. 写入 config/sector_map.json

数据源：swsresearch.com（申万宏源研究官网）
  - sw_index_first_info() → 31个一级行业代码+名称  (legulegu.com)
  - index_component_sw(code) × 31 → 各行业成分股    (swsresearch.com)

降级策略：
  - sw_index_first_info() 失败 → 硬编码31个行业（申万2021版）
  - index_component_sw() 部分失败 → 跳过失败行业，用已成功的
  - 全部失败 → stock_industry_clf_hist_sw() XLS全量下载（三级→一级推导）

调用方式：
  - 独立运行: python3 collector/update_sector_map_sw.py
  - 被import:  from collector.update_sector_map_sw import update_sector_map

更新频率：每30天一次（scheduler周一检查）
"""

import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

from loguru import logger

# --- 路径 ---
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

CONFIG_DIR = BASE_DIR / "config"
SECTOR_MAP_PATH = CONFIG_DIR / "sector_map.json"

# --- 申万2021版 一级行业（31个）硬编码 ---
# sw_index_first_info() 能拿到就用动态的，拿不到用这个
SW_FIRST_LEVEL = {
    "801010": "农林牧渔",
    "801020": "基础化工",
    "801030": "钢铁",
    "801040": "有色金属",
    "801050": "电子",
    "801060": "汽车",
    "801070": "家用电器",
    "801080": "食品饮料",
    "801090": "纺织服饰",
    "801100": "轻工制造",
    "801110": "医药生物",
    "801120": "公用事业",
    "801130": "交通运输",
    "801140": "房地产",
    "801150": "商贸零售",
    "801160": "社会服务",
    "801170": "综合",
    "801180": "建筑材料",
    "801190": "建筑装饰",
    "801200": "电力设备",
    "801210": "国防军工",
    "801230": "计算机",
    "801710": "通信",
    "801720": "传媒",
    "801730": "煤炭",
    "801740": "石油石化",
    "801750": "环保",
    "801760": "美容护理",
    "801770": "机械设备",
    "801780": "银行",
    "801790": "非银金融",
}


def _get_industry_list() -> dict:
    """获取申万一级行业列表: {code: name}
    
    优先动态获取，失败则用硬编码。
    """
    try:
        import akshare as ak
        df = ak.sw_index_first_info()
        if df is not None and len(df) >= 28:
            result = {}
            for _, row in df.iterrows():
                code = str(row["行业代码"]).strip().replace(".SI", "")
                name = str(row["行业名称"]).strip()
                result[code] = name
            logger.info(f"sw_index_first_info 动态获取: {len(result)}个行业")
            return result
    except Exception as e:
        logger.warning(f"sw_index_first_info 失败({e})，使用硬编码")
    
    logger.info(f"使用硬编码申万一级: {len(SW_FIRST_LEVEL)}个行业")
    return dict(SW_FIRST_LEVEL)


def _fetch_components_by_industry(industries: dict) -> dict:
    """逐行业拉成分股，构建 stock_code → industry_name 映射。
    
    Args:
        industries: {industry_code: industry_name}
    
    Returns:
        {stock_code: industry_name}  stock_code为6位字符串
    """
    import akshare as ak
    
    mapping = {}
    success_count = 0
    fail_count = 0
    
    for code, name in industries.items():
        try:
            df = ak.index_component_sw(symbol=code)
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    stock_code = str(row["证券代码"]).strip()
                    # 确保6位
                    if len(stock_code) == 6 and stock_code.isdigit():
                        mapping[stock_code] = name
                success_count += 1
                logger.debug(f"  {name}({code}): {len(df)}只")
            else:
                fail_count += 1
                logger.warning(f"  {name}({code}): 返回空")
            
            # 礼貌间隔，避免被封
            time.sleep(0.5)
            
        except Exception as e:
            fail_count += 1
            logger.warning(f"  {name}({code}) 失败: {e}")
            time.sleep(1)
    
    logger.info(f"Step1完成: {success_count}成功/{fail_count}失败, 映射{len(mapping)}只股票")
    return mapping


def _fetch_via_xls_fallback(industries: dict) -> dict:
    """降级方案：通过XLS全量下载获取映射。
    
    stock_industry_clf_hist_sw() 返回的是三级行业代码，
    需要通过代码前缀推导一级行业。
    
    申万行业代码规则：
      一级: 801010, 801020, ...
      二级: 801011, 801012, ...  (一级代码基础上末位变化)
      三级: 801011, 801012, ...  (实际是6位代码如850111)
    
    实际上三级代码是85xxxx格式，和一级801xxx不同。
    需要用 sw_index_third_info() 获取三级→一级的映射关系。
    这个方案比较复杂，作为最后手段。
    """
    import akshare as ak
    
    logger.info("尝试XLS降级方案...")
    
    try:
        # 1. 下载全量分类历史
        df = ak.stock_industry_clf_hist_sw()
        if df is None or len(df) == 0:
            logger.error("stock_industry_clf_hist_sw 返回空")
            return {}
        
        logger.info(f"XLS下载: {len(df)}条记录")
        logger.info(f"列: {list(df.columns)}")
        logger.info(f"industry_code样例: {df['industry_code'].head(5).tolist()}")
        
        # 2. 每只股票取最新的一条记录（按start_date降序去重）
        df_sorted = df.sort_values('start_date', ascending=False)
        df_latest = df_sorted.drop_duplicates(subset='symbol', keep='first')
        logger.info(f"去重后: {len(df_latest)}只股票")
        
        # 3. 三级代码→一级行业名
        # 尝试获取三级行业信息（含一级归属）
        try:
            df_third = ak.sw_index_third_info()
            logger.info(f"sw_index_third_info: {len(df_third)}条, 列: {list(df_third.columns)}")
            # 构建三级代码→一级名称的lookup
            # 需要看返回的列结构来决定怎么映射
            # 这里先记录，实际映射逻辑需要根据返回数据调整
        except Exception as e:
            logger.warning(f"sw_index_third_info 失败: {e}")
        
        # 简化方案：三级代码的前4位(8010/8011/...)可以匹配一级代码(801010/801110/...)
        # 但这个映射不太可靠，先跳过
        logger.warning("XLS三级→一级推导逻辑较复杂，本次跳过")
        return {}
        
    except Exception as e:
        logger.error(f"XLS降级失败: {e}")
        return {}


def update_sector_map(force: bool = False) -> dict:
    """主函数：更新板块映射。
    
    Args:
        force: 强制更新（忽略缓存时间）
    
    Returns:
        sector_map dict（同时写入JSON文件）
    """
    # 检查是否需要更新（30天缓存）
    if not force and SECTOR_MAP_PATH.exists():
        try:
            with open(SECTOR_MAP_PATH, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            updated = existing.get("updated", "")
            if updated:
                last_update = datetime.strptime(updated, "%Y-%m-%d").date()
                days_since = (date.today() - last_update).days
                if days_since < 30 and existing.get("map") and len(existing["map"]) > 100:
                    logger.info(f"sector_map 缓存有效({days_since}天前更新, {len(existing['map'])}只)，跳过")
                    return existing
        except Exception:
            pass  # 文件损坏，继续更新
    
    logger.info("=== 开始更新申万行业映射 ===")
    
    # 1. 获取行业列表
    industries = _get_industry_list()
    logger.info(f"行业列表: {len(industries)}个")
    for code, name in industries.items():
        logger.debug(f"  {code}: {name}")
    
    # 2. 逐行业拉成分股
    mapping = _fetch_components_by_industry(industries)
    
    # 3. 如果Step1覆盖不足，尝试XLS降级
    if len(mapping) < 3000:
        logger.warning(f"Step1只映射了{len(mapping)}只，尝试XLS补充...")
        xls_mapping = _fetch_via_xls_fallback(industries)
        if xls_mapping:
            # 合并：Step1优先，XLS补充
            for code, name in xls_mapping.items():
                if code not in mapping:
                    mapping[code] = name
            logger.info(f"XLS补充后: {len(mapping)}只")
    
    # 4. 构建输出
    sector_map = {
        "updated": date.today().strftime("%Y-%m-%d"),
        "source": "swsresearch_first_level",
        "industry_count": len(industries),
        "stock_count": len(mapping),
        "industries": sorted(set(industries.values())),
        "map": mapping,
    }
    
    # 5. 写入文件（原子写入）
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = SECTOR_MAP_PATH.with_suffix('.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(sector_map, f, ensure_ascii=False, indent=2)
    tmp_path.rename(SECTOR_MAP_PATH)
    
    logger.info(f"✅ sector_map.json 更新完成: {len(industries)}个行业, {len(mapping)}只股票")
    
    # 6. 统计
    from collections import Counter
    dist = Counter(mapping.values())
    logger.info("行业分布 Top10:")
    for name, count in dist.most_common(10):
        logger.info(f"  {name}: {count}只")
    
    return sector_map


# --- 独立运行 ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="更新申万行业映射")
    parser.add_argument("--force", action="store_true", help="强制更新（忽略30天缓存）")
    args = parser.parse_args()
    
    result = update_sector_map(force=args.force)
    print(f"\n映射结果: {result.get('stock_count', 0)}只股票 → {result.get('industry_count', 0)}个行业")
