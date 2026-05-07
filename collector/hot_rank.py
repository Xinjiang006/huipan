#!/usr/bin/env python3
"""
慧盘 · 雪球人气榜采集器
v3.1 — 3类×2子榜（新增+热门）

输出: static/data/hot_rank.json
键:
  follow_new  关注·本周新增
  follow_hot  关注·最热门
  tweet_new   讨论·本周新增
  tweet_hot   讨论·最热门
  deal_new    交易·本周新增
  deal_hot    交易·最热门
"""

import json, os, traceback
from datetime import datetime

TOP_N = 50
OUT = os.path.join(os.path.dirname(__file__), '..', 'static', 'data', 'hot_rank.json')

def clean_code(raw):
    """SH600519 → 600519"""
    if not raw:
        return ''
    return raw.replace('SH', '').replace('SZ', '').replace('BJ', '')

def fetch_list(func, symbol, top_n=TOP_N):
    """通用抓取+格式化"""
    try:
        df = func(symbol=symbol)
        if df is None or df.empty:
            return []
        # 列名容错
        code_col = next((c for c in df.columns if '代码' in c or 'code' in c.lower()), df.columns[1])
        name_col = next((c for c in df.columns if '名称' in c or '简称' in c or 'name' in c.lower()), df.columns[2])
        price_col = next((c for c in df.columns if '最新价' in c or 'price' in c.lower()), None)
        # value列: 关注/讨论/交易/新增 数值
        val_col = next((c for c in df.columns if c in ['关注', '讨论', '交易', '新增']), None)
        if val_col is None:
            # fallback: 最后一个数值列
            for c in reversed(df.columns):
                if df[c].dtype in ['int64', 'float64'] and c != price_col:
                    val_col = c
                    break

        result = []
        for _, row in df.head(top_n).iterrows():
            item = {
                'code': clean_code(str(row[code_col])),
                'name': str(row[name_col]),
                'price': float(row[price_col]) if price_col and row.get(price_col) is not None else None,
                'value': int(row[val_col]) if val_col and row.get(val_col) is not None else 0,
            }
            result.append(item)
        return result
    except Exception as e:
        print(f"  ⚠ {func.__name__}({symbol}) 失败: {e}")
        return []


def main():
    import akshare as ak

    print("=" * 60)
    print("慧盘 · 雪球人气榜采集")
    print("=" * 60)

    result = {}

    # ── 关注排行榜 ──
    print("  📡 关注·本周新增...")
    result['follow_new'] = fetch_list(ak.stock_hot_follow_xq, '本周新增')
    print(f"     → {len(result['follow_new'])} 条")

    print("  📡 关注·最热门...")
    result['follow_hot'] = fetch_list(ak.stock_hot_follow_xq, '最热门')
    print(f"     → {len(result['follow_hot'])} 条")

    # ── 讨论排行榜 ──
    print("  📡 讨论·本周新增...")
    result['tweet_new'] = fetch_list(ak.stock_hot_tweet_xq, '本周新增')
    print(f"     → {len(result['tweet_new'])} 条")

    print("  📡 讨论·最热门...")
    result['tweet_hot'] = fetch_list(ak.stock_hot_tweet_xq, '最热门')
    print(f"     → {len(result['tweet_hot'])} 条")

    # ── 交易排行榜 ──
    print("  📡 交易·本周新增...")
    result['deal_new'] = fetch_list(ak.stock_hot_deal_xq, '本周新增')
    print(f"     → {len(result['deal_new'])} 条")

    print("  📡 交易·最热门...")
    result['deal_hot'] = fetch_list(ak.stock_hot_deal_xq, '最热门')
    print(f"     → {len(result['deal_hot'])} 条")

    result['fetched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total = sum(len(result[k]) for k in result if isinstance(result[k], list))
    print(f"\n✅ 完成 · {total} 条 · {OUT}")

    # --- DuckDB 入库 (v3.4) ---
    try:
        from storage.duckdb_v3_tables import DuckDBV3Store
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        store = DuckDBV3Store(os.path.join(BASE_DIR, "data", "huipan.duckdb"))
        store.save_hot_rank(datetime.now().strftime('%Y-%m-%d'), result)
    except Exception as e:
        print(f"  ⚠️ DuckDB入库跳过: {e}")


if __name__ == '__main__':
    main()
