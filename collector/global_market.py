"""
慧盘 v2 · 全球行情采集器
collector/global_market.py

数据源：Sina 批量HTTP + AKShare（国债）
一次HTTP请求拿到全部行情，解析4种不同格式

已验证代码（2026-03-10 WSL + ECS）：
  gb_*    美股指数（纳斯达克/道琼斯/标普500）
  rt_hk*  港股指数（恒生/恒生科技）
  hf_*    期货商品（黄金/白银/原油/铜/铝/镍/锌/比特币）
  fx_*    汇率（美元/欧元/英镑/日元 兑人民币）

国债收益率：AKShare bond_zh_us_rate（无Sina替代）
美元指数DXY：暂无可用Sina代码，缺失

依赖：requests, akshare（仅国债）
"""

import re
import time
from datetime import date, datetime, timezone
from typing import Optional

import requests
import json


# ── 配置 ──────────────────────────────────────

SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ── 全部追踪代码 ──────────────────────────────

# (sina_code, display_name, category)
SYMBOLS = [
    # 美股指数 — gb_ 格式: 名称,价格,涨跌幅%,日期时间,...
    ("gb_$ixic",       "纳斯达克",     "us_index"),
    ("gb_$dji",        "道琼斯",       "us_index"),
    ("gb_$inx",        "标普500",      "us_index"),

    # 港股 — rt_hk 格式: code,名称,现价,昨收,最高,最低,...涨跌额,涨跌幅%,...
    ("rt_hkHSI",       "恒生指数",     "hk_index"),
    ("rt_hkHSTECH",    "恒生科技",     "hk_index"),
    ("rt_hkHSCEI",     "国企指数",     "hk_index"),
    ("rt_hkHSCCI",     "红筹指数",     "hk_index"),

    # 贵金属 — hf_ 格式: 现价,,买价,卖价,最高,最低,时间,昨结算,昨收,...
    ("hf_GC",          "黄金",         "precious_metal"),
    ("hf_SI",          "白银",         "precious_metal"),

    # 能源
    ("hf_CL",          "WTI原油",      "energy"),
    ("hf_OIL",         "布伦特原油",   "energy"),

    # 有色金属
    ("hf_HG",          "铜",           "base_metal"),
    ("hf_AHD",         "LME铝",        "base_metal"),
    ("hf_NID",         "LME镍",        "base_metal"),
    ("hf_ZSD",         "LME锌",        "base_metal"),

    # 汇率 — fx_ 格式: 时间,现价,买价,...
    ("fx_susdcny",     "美元/人民币",  "forex"),
    ("fx_seurcny",     "欧元/人民币",  "forex"),
    ("fx_sgbpcny",     "英镑/人民币",  "forex"),
    ("fx_sjpycny",     "日元/人民币",  "forex"),

    # 加密
    ("hf_BTC",         "比特币",       "crypto"),
]


# ── 前端右列分组 ──────────────────────────────

DISPLAY_GROUPS = [
    ("亚太",      ["恒生指数", "恒生科技", "国企指数", "红筹指数"]),
    ("美股",      ["纳斯达克", "标普500", "道琼斯"]),
    ("汇率",      ["美元/人民币", "欧元/人民币", "英镑/人民币", "日元/人民币"]),
    ("利率",      ["中国10年国债", "中国30年国债", "美债10年"]),
    ("贵金属",    ["黄金", "白银"]),
    ("能源",      ["布伦特原油", "WTI原油"]),
    ("有色",      ["铜", "LME铝", "LME镍", "LME锌"]),
    ("其他",      ["比特币"]),
]


# ── 工具函数 ──────────────────────────────────

def _safe_float(val, default=0.0) -> float:
    try:
        if val is None or str(val).strip() in ("", "-", "—", "nan"):
            return default
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return default


# ── Sina 批量采集 ──────────────────────────────

def _fetch_sina_batch(verbose: bool = True) -> list[dict]:
    """
    一次HTTP请求拿到全部Sina行情数据。
    根据代码前缀分别解析4种格式。
    """
    codes = [s[0] for s in SYMBOLS]
    url = f"http://hq.sinajs.cn/list={','.join(codes)}"

    if verbose:
        print(f"  📡 Sina 批量请求 ({len(codes)}个)...")

    try:
        resp = requests.get(url, headers=SINA_HEADERS, timeout=15)
        if resp.status_code != 200:
            if verbose:
                print(f"  ❌ HTTP {resp.status_code}")
            return []
    except Exception as e:
        if verbose:
            print(f"  ❌ 请求失败: {e}")
        return []

    # 建立 code → (name, category) 映射
    code_map = {s[0]: (s[1], s[2]) for s in SYMBOLS}

    results = []
    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # 提取代码和数据
        match = re.match(r'var hq_str_(.+?)="(.*)";', line)
        if not match:
            continue

        code = match.group(1)
        data = match.group(2)

        if not data or code not in code_map:
            continue

        name, category = code_map[code]
        parts = data.split(",")

        try:
            item = _parse_by_prefix(code, parts, name, category)
            if item:
                results.append(item)
        except Exception as e:
            if verbose:
                print(f"  ⚠️ 解析失败 {code}: {e}")

    if verbose:
        print(f"  ✅ {len(results)}/{len(codes)} 项成功")

    return results


def _parse_by_prefix(code: str, parts: list, name: str, category: str) -> Optional[dict]:
    """
    根据Sina代码前缀选择不同的解析逻辑
    """
    if code.startswith("gb_"):
        return _parse_gb(parts, name, category)
    elif code.startswith("rt_hk"):
        return _parse_rt_hk(parts, name, category)
    elif code.startswith("hf_"):
        return _parse_hf(parts, name, category)
    elif code.startswith("fx_"):
        return _parse_fx(parts, name, category)
    return None


def _parse_gb(parts: list, name: str, category: str) -> Optional[dict]:
    """
    gb_ 美股指数格式：名称,价格,涨跌幅%,日期时间,...
    例: 纳斯达克,22836.08,0.62,2026-03-10 23:49:46,...
    """
    if len(parts) < 3:
        return None
    price = _safe_float(parts[1])
    change_pct = _safe_float(parts[2])
    if price == 0:
        return None
    return {
        "name": name,
        "category": category,
        "value": price,
        "change_pct": change_pct,
    }


def _parse_rt_hk(parts: list, name: str, category: str) -> Optional[dict]:
    """
    rt_hk 港股格式：code,名称,现价,昨收,最高,最低,...涨跌额,涨跌幅%,...
    例: HSI,恒生指数,25740.290,25408.461,...,551.440,2.170,...
    """
    if len(parts) < 8:
        return None
    price = _safe_float(parts[2])
    prev_close = _safe_float(parts[3])
    change_pct = _safe_float(parts[8])

    # 如果涨跌幅为0但有价格和昨收，自行计算
    if change_pct == 0 and price > 0 and prev_close > 0:
        change_pct = round((price - prev_close) / prev_close * 100, 2)

    if price == 0:
        return None
    return {
        "name": name,
        "category": category,
        "value": price,
        "change_pct": change_pct,
    }


def _parse_hf(parts: list, name: str, category: str) -> Optional[dict]:
    """
    hf_ 期货格式：现价,,买价,卖价,最高,最低,时间,昨结算,昨收,...
    例: 5243.422,,5242.500,5243.000,5248.700,5127.100,23:49:42,5103.700,5152.400,...
    涨跌幅需要自行计算：(现价 - 昨结算) / 昨结算 * 100
    """
    if len(parts) < 8:
        return None
    price = _safe_float(parts[0])
    prev_settle = _safe_float(parts[7])  # 昨结算

    if price == 0:
        return None

    change_pct = 0.0
    if prev_settle > 0:
        change_pct = round((price - prev_settle) / prev_settle * 100, 2)

    return {
        "name": name,
        "category": category,
        "value": price,
        "change_pct": change_pct,
    }


def _parse_fx(parts: list, name: str, category: str) -> Optional[dict]:
    """
    fx_ 汇率格式：时间,现价,买价,...
    例: 23:47:02,6.8628000000,6.8680000000,...
    汇率没有直接的涨跌幅，用 (现价 - 昨收) 计算
    """
    if len(parts) < 6:
        return None
    price = _safe_float(parts[1])
    prev_close = _safe_float(parts[5])  # 昨收价

    if price == 0:
        return None

    change_pct = 0.0
    if prev_close > 0:
        change_pct = round((price - prev_close) / prev_close * 100, 2)

    return {
        "name": name,
        "category": category,
        "value": price,
        "change_pct": change_pct,
    }


# ── 国债收益率（保留AKShare）──────────────────

def _fetch_bond_rates(verbose: bool = True) -> list[dict]:
    """
    中美国债收益率 — AKShare bond_zh_us_rate
    无Sina替代，保留原有方案
    change_pct 存 bp 变化（基点）
    """
    try:
        import akshare as ak
        if verbose:
            print("  📡 AKShare 国债收益率...")

        df = ak.bond_zh_us_rate()
        if df is None or df.empty:
            if verbose:
                print("  ❌ 空数据")
            return []

        col_map = {
            "中国国债收益率10年": ("中国10年国债", "bond"),
            "中国国债收益率30年": ("中国30年国债", "bond"),
            "美国国债收益率10年": ("美债10年", "bond"),
        }

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest

        results = []
        for col, (display_name, category) in col_map.items():
            if col not in df.columns:
                continue
            val = _safe_float(latest.get(col))
            prev_val = _safe_float(prev.get(col))
            bp_chg = round((val - prev_val) * 100, 1)  # 基点变化
            results.append({
                "name": display_name,
                "category": category,
                "value": val,
                "change_pct": bp_chg,  # 注意：这里存的是bp，不是%
            })

        if verbose:
            print(f"  ✅ {len(results)} 条")
        return results

    except Exception as e:
        if verbose:
            print(f"  ❌ 国债采集失败: {e}")
        return []


# ── 分组组装（前端右列）──────────────────────

def assemble_groups(items: list[dict]) -> list[dict]:
    """
    将采集结果按前端右列分组顺序组装
    缺失的项自动跳过
    """
    idx = {item["name"]: item for item in items}
    result = []
    for title, names in DISPLAY_GROUPS:
        group_items = [idx[n] for n in names if n in idx]
        if group_items:
            result.append({"title": title, "items": group_items})
    return result


# ── 主入口 ──────────────────────────────────

def collect_global(verbose: bool = True) -> list[dict]:
    """
    采集全部全球行情数据

    返回:
        [{name, category, value, change_pct}, ...]

    用途：
        1. 入库 global_market 表（日频快照）
        2. 前端右列实时显示（通过 assemble_groups 分组）
    """
    if verbose:
        print("\n🌍 全球行情采集")
        print("-" * 40)

    # Sina 批量（一次HTTP）
    items = _fetch_sina_batch(verbose=verbose)

    # AKShare 国债（独立请求）
    bonds = _fetch_bond_rates(verbose=verbose)
    items.extend(bonds)

    if verbose:
        print(f"\n  📊 合计: {len(items)} 项")

    return items


def run(trade_date: Optional[date] = None, verbose: bool = True) -> dict:
    """
    兼容v1的调用接口 + 分组输出

    返回:
    {
        "date": "2026-03-10",
        "items": [...],
        "groups": [{"title": "美股", "items": [...]}, ...],
    }
    """
    items = collect_global(verbose=verbose)
    groups = assemble_groups(items)

    return {
        "date": str(trade_date or date.today()),
        "items": items,
        "groups": groups,
    }


# ── CLI 测试 ──────────────────────────────────
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "data")
JSON_FILE = os.path.join(DATA_DIR, "global_market.json")


def save_json(items: list, verbose: bool = True):
    """写入JSON，格式与data_export.export_global一致"""
    os.makedirs(DATA_DIR, exist_ok=True)

    # 按category分组（与data_export.py一致）
    by_category = {}
    for item in items:
        cat = item.get("category", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(item)

    export_data = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "items": items,
        "by_category": by_category,
    }

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"  💾 已写入 {JSON_FILE}")


if __name__ == "__main__":
    print("=" * 60)
    print("慧盘 v2 · 全球行情采集")
    print("=" * 60)

    data = run(verbose=True)
    items = data.get("items", [])

    if not items:
        print("⚠️ 采集失败，保留旧JSON")
        exit(1)

    save_json(items, verbose=True)

    print(f"\n日期: {data['date']}  |  总计: {len(items)} 项")
    for group in data["groups"]:
        print(f"\n  {group['title']}")
        for item in group["items"]:
            val_str = f"{item['value']:>12,.2f}" if item["value"] >= 100 else f"{item['value']:>12.4f}"
            chg = item["change_pct"]
            if item.get("category") == "bond":
                chg_str = f"{chg:+.1f}bp"
            else:
                chg_str = f"{chg:+.2f}%"
            print(f"    {item['name']:12s} {val_str}  {chg_str}")

    print(f"\n✅ 完成")
