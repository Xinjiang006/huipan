"""
慧盘 · Source层 · 全市场行情
AKShare stock_zh_a_spot()（Sina源），内置pkl缓存。

使用:
    from sources.spot import fetch_spot, load_spot

    # 强制拉取（movers用，每次都调API + 更新缓存）
    df = fetch_spot()

    # 优先读缓存（overview用，10分钟内复用）
    df = load_spot()
"""

import os
import time
import pickle
import akshare as ak

# ── 配置 ──────────────────────────────────────
# 缓存路径和TTL由外部设置，提供默认值
_CACHE_PATH = None
_CACHE_TTL = 600  # 10分钟


def configure(cache_path: str = None, cache_ttl: int = None):
    """设置缓存路径和TTL（采集器启动时调用一次）"""
    global _CACHE_PATH, _CACHE_TTL
    if cache_path is not None:
        _CACHE_PATH = cache_path
    if cache_ttl is not None:
        _CACHE_TTL = cache_ttl


def _get_cache_path() -> str:
    """获取缓存路径，未配置时用默认值"""
    if _CACHE_PATH:
        return _CACHE_PATH
    # 默认: 脚本所在目录的上级/static/data/.spot_cache.pkl
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "static", "data", ".spot_cache.pkl")


# ── 核心函数 ──────────────────────────────────

def fetch_spot() -> 'pd.DataFrame':
    """强制拉取全市场行情（调API + 写缓存）

    Returns:
        AKShare stock_zh_a_spot() 原始DataFrame
        列: 代码/名称/最新价/涨跌额/涨跌幅/买入/卖出/昨收/今开/最高/最低/成交量/成交额/时间戳
        代码带交易所前缀（sh/sz/bj）
    """
    print("[sources.spot] 获取全市场行情 (stock_zh_a_spot)...")
    t0 = time.time()
    df = ak.stock_zh_a_spot()
    elapsed = time.time() - t0
    print(f"  ✅ {len(df)}只, 耗时{elapsed:.1f}s")

    # 写缓存
    cache_path = _get_cache_path()
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump({"time": time.time(), "df": df}, f)
    print(f"  → 已缓存到 {cache_path}")

    return df


def load_spot() -> 'pd.DataFrame':
    """优先读缓存，过期则重新拉取

    缓存有效期内（默认10分钟）直接返回pkl中的DataFrame，
    过期或不存在时调用 fetch_spot()。

    Returns:
        同 fetch_spot()
    """
    cache_path = _get_cache_path()

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cache = pickle.load(f)
            age = time.time() - cache["time"]
            if age < _CACHE_TTL:
                print(f"  ✅ 复用缓存 ({age:.0f}s前)")
                return cache["df"]
            print(f"  ⚠️ 缓存过期 ({age:.0f}s)")
        except Exception as e:
            print(f"  ⚠️ 缓存读取失败: {e}")

    return fetch_spot()
