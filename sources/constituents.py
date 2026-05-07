"""
慧盘 · Source层 · 指数成分股
AKShare index_stock_cons()，30天本地缓存。

使用:
    from sources.constituents import load_index_constituents
    code_to_cap = load_index_constituents()
    # {'600519': 'large', '000858': 'large', ...}
"""

import os
import json
import time

_CACHE_PATH = None
_CACHE_DAYS = 30


def configure(cache_path: str = None, cache_days: int = None):
    """设置缓存路径和有效期"""
    global _CACHE_PATH, _CACHE_DAYS
    if cache_path is not None:
        _CACHE_PATH = cache_path
    if cache_days is not None:
        _CACHE_DAYS = cache_days


def _get_cache_path() -> str:
    if _CACHE_PATH:
        return _CACHE_PATH
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "index_constituents.json")


def load_index_constituents() -> dict:
    """获取三大指数成分股代码 → 市值标签映射，带本地缓存

    Returns:
        {'600519': 'large', '002415': 'mid', '301236': 'small', ...}
        未在三大指数中的股票不在返回值中（隐含为micro）
    """
    import akshare as ak

    cache_path = _get_cache_path()

    # 1. 读缓存
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            age_days = (time.time() - cache.get("time", 0)) / 86400
            if age_days < _CACHE_DAYS:
                code_to_cap = cache.get("code_to_cap", {})
                if code_to_cap:
                    print(f"  ✅ 成分股缓存 ({age_days:.0f}天前, {len(code_to_cap)}只)")
                    return code_to_cap
            print(f"  ⚠️ 成分股缓存过期 ({age_days:.0f}天)")
        except Exception as e:
            print(f"  ⚠️ 成分股缓存读取失败: {e}")

    # 2. 在线获取
    INDEX_MAP = [
        ("large", "000300", "沪深300"),
        ("mid",   "000905", "中证500"),
        ("small", "000852", "中证1000"),
    ]
    code_to_cap = {}
    for cap_key, idx_code, idx_name in INDEX_MAP:
        try:
            cons = ak.index_stock_cons(symbol=idx_code)
            code_col = next((c for c in cons.columns if "代码" in c), cons.columns[0])
            codes = set(str(c).zfill(6) for c in cons[code_col])
            for c in codes:
                if c not in code_to_cap:
                    code_to_cap[c] = cap_key
            print(f"    {idx_name}: {len(codes)}只")
            time.sleep(0.3)
        except Exception as e:
            print(f"    ❌ {idx_name}: {e}")

    # 3. 保存缓存
    if code_to_cap:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "code_to_cap": code_to_cap}, f)
        print(f"  ✅ 成分股已缓存 ({len(code_to_cap)}只 → {cache_path})")
        return code_to_cap

    # 4. 在线失败，回退过期缓存
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            code_to_cap = cache.get("code_to_cap", {})
            if code_to_cap:
                print(f"  ⚠️ 在线获取失败，使用过期缓存 ({len(code_to_cap)}只)")
                return code_to_cap
        except Exception:
            pass

    return {}
