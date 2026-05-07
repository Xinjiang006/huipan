"""
慧盘 v2 · 港股数据采集器
collector/hk_market.py

数据源：新浪港股通排行API（Market_Center.getHKStockData）
  - 涨幅榜：node=sgt_hk, sort=changepercent, asc=0
  - 跌幅榜：node=sgt_hk, sort=changepercent, asc=1
  - 成交额榜：node=sgt_hk, sort=amount, asc=0
  - 热门港股：node=hot_hk, sort=amount, asc=0

v3.5 重写：从 stock_hk_spot() 全量99页 → 4次排行API调用（港股通~700只）
  解决高频请求被Sina封IP问题（99次→4次，降低96%请求量）
依赖：requests, json
"""

from datetime import datetime, timezone
import os
import time

from utils.huipan_utils import *
# ── API配置 ──────────────────────────────────────

SINA_API = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHKStockData"
)

SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}

# 每个榜单的API参数
ENDPOINTS = {
    "hk_gainers": {"sort": "changepercent", "asc": 0, "node": "sgt_hk"},
    "hk_losers":  {"sort": "changepercent", "asc": 1, "node": "sgt_hk"},
    "hk_volume":  {"sort": "amount",        "asc": 0, "node": "sgt_hk"},
    "hk_hot":     {"sort": "amount",        "asc": 0, "node": "hot_hk"},
}


# ── 工具函数 ────────────────────────────────────

def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _parse_items(raw_items: list) -> list:
    """将Sina API返回的原始JSON转为慧盘标准格式"""
    result = []
    for item in raw_items:
        try:
            price = _safe_float(item.get("lasttrade"))
            change = _safe_float(item.get("pricechange"))
            change_pct = _safe_float(item.get("changepercent"))
            amount = _safe_float(item.get("amount"))

            # 跳过无效数据
            if price <= 0:
                continue

            result.append({
                "code": str(item.get("symbol", "")).zfill(5),
                "name": str(item.get("name", "")),
                "price": round(price, 3),
                "change": round(change, 3),
                "change_pct": round(change_pct, 2),
                "volume_hkd": round(amount / 1e8, 2),  # 转亿港元
            })
        except Exception:
            continue
    return result





# ── 主采集函数 ───────────────────────────────────

def collect_hk_movers(top_n: int = 50, verbose: bool = True) -> dict:
    """
    采集港股涨跌幅/成交额/热门排行

    返回:
    {
        "hk_gainers": [{code, name, price, change, change_pct, volume_hkd}, ...],
        "hk_losers": [...],
        "hk_volume": [...],
        "hk_hot": [...],
        "fetched_at": "2026-03-16T08:00:00Z",
    }

    总请求次数：4次（对比旧版99次）
    """
    if verbose:
        print("  📡 港股排行数据 (Sina排行API)...")

    result = {}
    for key in ["hk_gainers", "hk_losers", "hk_volume", "hk_hot"]:

        cfg = ENDPOINTS[key]
        params = {
            "page": 1,
            "num": top_n,
            "sort": cfg["sort"],
            "asc": cfg["asc"],
            "node": cfg["node"],
        }

        resp = fetch_url(SINA_API, SINA_HEADERS, params)
        result[key] = _parse_items(json.loads(resp))


    result["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if verbose:
        total = sum(len(result[k]) for k in ["hk_gainers", "hk_losers", "hk_volume", "hk_hot"])
        print(f"  📊 港股合计 {total} 条（4次API调用）")

    return result


# ── DuckDB入库 ───────────────────────────────────

def save_to_db(data: dict, db_path: str = None, verbose: bool = True) -> int:
    """将港股榜单写入 hk_stock_movers 表"""
    if not data:
        if verbose:
            print("  ⚠️ save_hk_to_db: 空数据，跳过")
        return 0

    try:
        import duckdb
    except ImportError:
        if verbose:
            print("  ⚠️ duckdb 未安装，跳过入库")
        return 0

    if db_path is None:
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "huipan.duckdb")

    from datetime import date as _date
    trade_date = str(_date.today())

    con = duckdb.connect(db_path)
    total = 0

    for mover_type in ["hk_gainers", "hk_losers", "hk_volume", "hk_hot"]:
        items = data.get(mover_type, [])
        for item in items:
            try:
                con.execute("""
                    INSERT OR REPLACE INTO hk_stock_movers
                        (date, code, name, price, change, change_pct, volume_hkd, mover_type, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    trade_date,
                    item["code"],
                    item["name"],
                    item["price"],
                    item["change"],
                    item["change_pct"],
                    item["volume_hkd"],
                    mover_type,
                    data.get("fetched_at", ""),
                ])
                total += 1
            except Exception as e:
                if verbose:
                    print(f"  ⚠️ 入库失败 {item.get('code')}: {e}")

    con.close()
    if verbose:
        print(f"  💾 港股入库 {total} 条")
    return total


# ── CLI测试 ──────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("慧盘 · 港股通数据采集测试 (v3.5 · Sina排行API)")
    print("=" * 60)

    data = collect_hk_movers(top_n=50, verbose=True)

    for category in ["hk_gainers", "hk_losers", "hk_volume", "hk_hot"]:
        items = data.get(category, [])
        label = {"hk_gainers": "涨幅榜", "hk_losers": "跌幅榜",
                 "hk_volume": "成交额榜", "hk_hot": "热门"}[category]
        print(f"\n── {label} (Top 5) ──")
        for i, r in enumerate(items[:5], 1):
            print(f"  {i:>2}. {r['code']} {r['name']:10s}"
                  f"  {r['price']:>8.3f}"
                  f"  {r['change_pct']:+.2f}%"
                  f"  {r['volume_hkd']:.1f}亿")

    # 写JSON
    save_json(data, "hk_movers.json")

    print(f"\n✅ 完成 · {data.get('fetched_at', '')}")
    print("📌 总请求：4次（旧版99次）")
