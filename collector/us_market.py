"""
慧盘 v2 · 美股数据采集器
collector/us_market.py

数据源（已验证可用）：
  - Yahoo Finance 榜单页（gainers / losers / most-active / trending）
  - Sina 批量接口（21只板块ETF，0.1秒全部返回）

依赖：requests, beautifulsoup4
"""

import re
import time
from datetime import datetime, date, timezone

import requests
from bs4 import BeautifulSoup

from utils.huipan_utils import *

# ── 配置 ──────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}


# ── 板块 ETF 清单 ──────────────────────────────

SECTOR_ETFS = {
    # 11 大板块
    "XLK":  {"name": "科技",     "name_en": "Technology"},
    "XLF":  {"name": "金融",     "name_en": "Financials"},
    "XLE":  {"name": "能源",     "name_en": "Energy"},
    "XLI":  {"name": "工业",     "name_en": "Industrials"},
    "XLV":  {"name": "医疗",     "name_en": "Healthcare"},
    "XLP":  {"name": "必需消费",  "name_en": "Consumer Staples"},
    "XLY":  {"name": "可选消费",  "name_en": "Consumer Discretionary"},
    "XLB":  {"name": "材料",     "name_en": "Materials"},
    "XLU":  {"name": "公用事业",  "name_en": "Utilities"},
    "XLRE": {"name": "房地产",   "name_en": "Real Estate"},
    "XLC":  {"name": "通信",     "name_en": "Communication"},
    # 主题 ETF
    "SOXX": {"name": "半导体",   "name_en": "Semiconductors"},
    "ITA":  {"name": "军工",     "name_en": "Defense"},
    "TAN":  {"name": "光伏",     "name_en": "Solar"},
    "ARKK": {"name": "创新科技",  "name_en": "Innovation"},
    "GDX":  {"name": "黄金矿业",  "name_en": "Gold Miners"},
    "USO":  {"name": "原油",     "name_en": "Crude Oil"},
    "URA":  {"name": "铀/核能",  "name_en": "Uranium"},
    "REMX": {"name": "稀土",     "name_en": "Rare Earth"},
    "LIT":  {"name": "锂电",     "name_en": "Lithium"},
    "SLV":  {"name": "白银",     "name_en": "Silver"},
}


# ── Yahoo 榜单页 ──────────────────────────────

YAHOO_MOVERS = {
    "gainers":     "https://finance.yahoo.com/markets/stocks/gainers/?count=50",
    "losers":      "https://finance.yahoo.com/markets/stocks/losers/?count=50",
    "most_active": "https://finance.yahoo.com/markets/stocks/most-active/?count=50",
    "trending":    "https://finance.yahoo.com/markets/stocks/trending/?count=50",
}


# ══════════════════════════════════════════════
#  Part 1: Yahoo Finance 榜单（涨幅/跌幅/量/热门）
# ══════════════════════════════════════════════

def _parse_yahoo_table(html: str) -> list[dict]:
    """
    解析 Yahoo Finance 表格页
    返回 [{symbol, name, price, change, change_pct}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue

        texts = [c.get_text(strip=True) for c in cells]

        # 跳过表头
        if texts[0] in ("Symbol", "Name", ""):
            continue

        symbol = texts[0]
        # 股票代码校验：1-5个大写字母
        if not re.match(r'^[A-Z]{1,5}$', symbol):
            continue

        name = texts[1] if len(texts) > 1 else ""

        # 价格和涨跌幅在后续列，格式不固定，尝试提取数字
        price = 0.0
        change = 0.0
        change_pct = 0.0

        for t in texts[2:]:
            # 匹配 "22.40+6.66(+42.31%)" 这种组合格式
            combo = re.search(r'([\d.]+)([+-][\d.]+)\(([+-][\d.]+)%\)', t)
            if combo:
                price = float(combo.group(1))
                change = float(combo.group(2))
                change_pct = float(combo.group(3))
                break

            # 匹配纯百分比 "+42.31%"
            pct_match = re.search(r'([+-]?[\d.]+)%', t)
            if pct_match and change_pct == 0:
                change_pct = float(pct_match.group(1))

            # 匹配纯价格 "22.40"
            price_match = re.match(r'^[\d,.]+$', t.replace(',', ''))
            if price_match and price == 0:
                try:
                    price = float(t.replace(',', ''))
                except ValueError:
                    pass

        # 提取成交量（格式如 "385.21M", "1.2B", "45.6K"）
        volume_str = ""
        for t in texts[2:]:
            vol_match = re.search(r'^([\d,.]+)\s*([MBK])$', t.strip(), re.IGNORECASE)
            if vol_match:
                volume_str = t.strip()
                break

        # 提取成交量（如 385.21M, 1.2B, 45.6K）
        volume_str = ""
        for t in texts[2:]:
            vol_match = re.search(r'^([\d,.]+)\s*([MBK])$', t.strip(), re.IGNORECASE)
            if vol_match:
                volume_str = t.strip()
                break

        if symbol and price > 0:
            item = {
                "symbol": symbol,
                "name": name,
                "price": price,
                "change": change,
                "change_pct": change_pct,
            }
            if volume_str:
                item["volume_str"] = volume_str
            rows.append(item)

    return rows


def collect_yahoo_movers(verbose: bool = True) -> dict:
    """
    采集 Yahoo Finance 四个榜单

    返回:
    {
        "gainers": [{symbol, name, price, change, change_pct}, ...],
        "losers": [...],
        "most_active": [...],
        "trending": [...],
        "fetched_at": "2026-03-09T06:00:00Z",
    }
    """
    result = {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    for mover_type, url in YAHOO_MOVERS.items():
        if verbose:
            print(f"  📊 {mover_type}...")

        html = fetch_url(url, HEADERS)
        if not html:
            result[mover_type] = []
            continue

        rows = _parse_yahoo_table(html)
        result[mover_type] = rows

        if verbose:
            print(f"     {len(rows)} 只 | 前3: {', '.join(r['symbol'] for r in rows[:3])}")

        time.sleep(1.5)

    return result


# ══════════════════════════════════════════════
#  Part 2: Sina 板块 ETF 批量行情
# ══════════════════════════════════════════════

def collect_sina_etfs(verbose: bool = True) -> list[dict]:
    """
    通过 Sina 批量接口获取 21 只板块 ETF 行情
    一个 HTTP 请求拿到全部

    返回:
    [{
        "symbol": "XLK",
        "name": "科技",
        "name_cn": "SPDR科技ETF",
        "close": 138.29,
        "change_pct": 0.73,
        "date": "2026-03-07",
    }, ...]
    """
    symbols_str = ",".join(f"gb_{s.lower()}" for s in SECTOR_ETFS.keys())
    url = f"http://hq.sinajs.cn/list={symbols_str}"

    if verbose:
        print(f"  📡 Sina 批量请求 ({len(SECTOR_ETFS)}只)...")

    text = fetch_url(url, headers=SINA_HEADERS, timeout=10)
    if not text:
        return []

    results = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        match = re.match(r'var hq_str_gb_(\w+)="(.*)";', line)
        if not match:
            continue

        code = match.group(1).upper()
        data = match.group(2)

        if not data:
            if verbose:
                print(f"     ❌ {code} 空数据")
            continue

        parts = data.split(",")
        etf_info = SECTOR_ETFS.get(code, {})

        # Sina 美股字段：名称,价格,涨跌幅%,日期时间,...
        try:
            name_cn = parts[0] if parts else ""
            close = float(parts[1]) if len(parts) > 1 else 0
            change_pct = float(parts[2]) if len(parts) > 2 else 0
            date_str = parts[3].split(" ")[0] if len(parts) > 3 else ""
        except (ValueError, IndexError):
            close, change_pct, date_str = 0, 0, ""

        results.append({
            "symbol": code,
            "name": etf_info.get("name", ""),
            "name_en": etf_info.get("name_en", ""),
            "name_cn": name_cn,
            "close": close,
            "change_pct": change_pct,
            "date": date_str,
        })

    if verbose:
        ok = len(results)
        print(f"     {ok}/{len(SECTOR_ETFS)} 只成功")

    return results


# ══════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════

def collect_all(verbose: bool = True) -> dict:
    """
    采集全部美股数据

    返回:
    {
        "movers": {gainers, losers, most_active, trending},
        "etfs": [{symbol, name, close, change_pct, ...}],
        "fetched_at": "...",
    }
    """
    if verbose:
        print("\n🇺🇸 美股数据采集")
        print("-" * 40)

    if verbose:
        print("\n[1/2] Yahoo Finance 榜单")
    movers = collect_yahoo_movers(verbose=verbose)

    if verbose:
        print("\n[2/2] Sina 板块 ETF")
    etfs = collect_sina_etfs(verbose=verbose)

    return {
        "movers": movers,
        "etfs": etfs,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── CLI 测试 ──────────────────────────────────

if __name__ == "__main__":

    print("=" * 60)
    print("慧盘 v2 · 美股数据采集测试")
    print("=" * 60)

    data = collect_all(verbose=True)

    # 汇总
    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)

    movers = data.get("movers")
    fetched_at = data.get("fetched_at")
    for mtype in ["gainers", "losers", "most_active", "trending"]:
        items = movers.get(mtype, [])
        print(f"\n  {mtype.upper()} ({len(items)}只):")
        for r in items[:5]:
            print(f"    {r['symbol']:6s} | {r['name'][:25]:25s} | ${r['price']:>8.2f} | {r['change_pct']:+.2f}%")

    print(f"\n  ETF ({len(data['etfs'])}只):")
    # 按涨跌幅排序
    sorted_etfs = sorted(data["etfs"], key=lambda x: x["change_pct"], reverse=True)
    for e in sorted_etfs[:5]:
        print(f"    {e['symbol']:5s} ({e['name']:6s}) | ${e['close']:>8.2f} | {e['change_pct']:+.2f}%")
    print("    ...")
    for e in sorted_etfs[-3:]:
        print(f"    {e['symbol']:5s} ({e['name']:6s}) | ${e['close']:>8.2f} | {e['change_pct']:+.2f}%")

    save_json(movers, "us_movers.json")
    save_json({"items": sorted_etfs, "count": len(sorted_etfs)}, "us_sectors.json")
