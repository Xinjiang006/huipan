"""
全球行情适配器 — 纯采集层，不做业务处理

接口清单：
  主：Yahoo Finance               → 美股指数（道琼/标普/纳指/VIX/日经）
  主：stock_hk_index_spot_sina    → 港股指数（恒生/恒生科技）
  主：futures_foreign_commodity_realtime → 外盘大宗（黄金/白银/原油/铜/铝）
  主：bond_zh_us_rate             → 中美国债收益率
  备：crypto_js_spot              → 加密货币（比特币）

注意：
  - index_global_spot_em 东财封锁，已弃用
  - futures_zh_spot 接口签名已变，已弃用
"""
import requests
from loguru import logger

# Yahoo Finance symbol 映射
_YAHOO_SYMBOLS = {
    "^DJI":  "道琼斯",
    "^GSPC": "标普500",
    "^IXIC": "纳斯达克",
    "^VIX":  "VIX恐慌",
    "^N225": "日经225",
}

# 港股指数映射（stock_hk_index_spot_sina 的"代码"字段）
_HK_INDEX_MAP = {
    "HSI":    "恒生指数",
    "HSTECH": "恒生科技",
}

# 外盘期货 symbol 映射（futures_foreign_commodity_realtime）
# 经测试可用的 symbol：XAU/XAG/OIL/CL/CAD/AHD
_FOREIGN_FUTURES = [
    ("XAU",  "黄金",    "commodity"),
    ("XAG",  "白银",    "commodity"),
    ("OIL",  "布伦特原油", "energy"),
    ("CL",   "WTI原油",  "energy"),
    ("CAD",  "LME铜",   "metal"),
    ("AHD",  "LME铝",   "metal"),
]


def _safe_float(val, default=0.0) -> float:
    try:
        if val is None or str(val).strip() in ("", "-", "—", "nan"):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def get_us_indices() -> list:
    """
    美股指数 + VIX — Yahoo Finance
    主接口：query1.finance.yahoo.com
    备接口：query2.finance.yahoo.com（相同格式，备用域名）
    返回: [{"name": "道琼斯", "value": 47501.55, "change_pct": -2.54, "group": "us_index"}, ...]
    """
    results = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for sym, name in _YAHOO_SYMBOLS.items():
        fetched = False
        for host in ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]:
            try:
                url = f"https://{host}/v8/finance/chart/{sym}?interval=1d&range=2d"
                r = requests.get(url, headers=headers, timeout=8)
                data = r.json()
                meta = data["chart"]["result"][0]["meta"]
                price = _safe_float(meta.get("regularMarketPrice"))
                prev = _safe_float(meta.get("chartPreviousClose"))
                chg = round((price - prev) / prev * 100, 2) if prev else 0.0
                results.append({
                    "name": name,
                    "value": price,
                    "change_pct": chg,
                    "group": "us_index",
                })
                fetched = True
                break
            except Exception as e:
                logger.warning(f"Yahoo {sym} via {host} 失败: {e}")

        if not fetched:
            logger.error(f"美股指数 {name}({sym}) 所有接口均失败")

    logger.debug(f"美股指数: {len(results)} 条")
    return results


def get_hk_indices() -> list:
    """
    港股指数 — 新浪
    返回: [{"name": "恒生指数", "value": ..., "change_pct": ..., "group": "hk_index"}, ...]
    """
    try:
        import akshare as ak
        df = ak.stock_hk_index_spot_sina()
        results = []
        for _, r in df.iterrows():
            code = str(r.get("代码", ""))
            if code in _HK_INDEX_MAP:
                # 计算涨跌幅：(最新价 - 昨收) / 昨收 * 100
                price = _safe_float(r.get("最新价"))
                prev = _safe_float(r.get("昨收"))
                chg = round((price - prev) / prev * 100, 2) if prev else 0.0
                results.append({
                    "name": _HK_INDEX_MAP[code],
                    "value": price,
                    "change_pct": chg,
                    "group": "hk_index",
                })
        logger.debug(f"港股指数: {len(results)} 条")
        return results
    except Exception as e:
        logger.error(f"港股指数采集失败: {e}")
        return []


def get_foreign_futures() -> list:
    """
    外盘大宗商品 — 新浪
    逐个 symbol 采集，单个失败不影响其他
    返回: [{"name": "黄金", "value": ..., "change_pct": ..., "group": "commodity"}, ...]
    """
    try:
        import akshare as ak
        results = []
        for sym, name, group in _FOREIGN_FUTURES:
            try:
                df = ak.futures_foreign_commodity_realtime(symbol=sym)
                if df is not None and not df.empty:
                    r = df.iloc[0]
                    results.append({
                        "name": name,
                        "value": _safe_float(r.get("最新价")),
                        "change_pct": _safe_float(r.get("涨跌幅")),
                        "group": group,
                    })
            except Exception as e:
                logger.warning(f"外盘期货 {name}({sym}) 失败: {e}")

        logger.debug(f"外盘大宗: {len(results)} 条")
        return results
    except Exception as e:
        logger.error(f"外盘大宗采集失败: {e}")
        return []


def get_bond_rates() -> list:
    """
    中美国债收益率 — AKShare
    返回: [{"name": "中国10年国债", "value": 1.82, "change_pct": -2.0, "group": "rate", "unit": "%"}, ...]
    change_pct 字段存 bp 变化（基点）
    """
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        if df is None or df.empty:
            return []

        col_map = {
            "中国国债收益率10年": ("中国10年国债", "rate"),
            "中国国债收益率30年": ("中国30年国债", "rate"),
            "美国国债收益率10年": ("美债10年", "rate"),
        }

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest

        results = []
        for col, (display, group) in col_map.items():
            if col not in df.columns:
                continue
            val = _safe_float(latest.get(col))
            prev_val = _safe_float(prev.get(col))
            bp_chg = round((val - prev_val) * 100, 1)
            results.append({
                "name": display,
                "value": val,
                "change_pct": bp_chg,   # 单位 bp
                "group": group,
                "unit": "%",
                "unit_chg": "bp",
            })

        logger.debug(f"国债收益率: {len(results)} 条")
        return results
    except Exception as e:
        logger.error(f"国债收益率采集失败: {e}")
        return []


def get_crypto() -> list:
    """
    加密货币 — JS数据源
    返回: [{"name": "比特币", "value": ..., "change_pct": ..., "group": "crypto"}, ...]
    """
    try:
        import akshare as ak
        df = ak.crypto_js_spot()
        if df is None or df.empty:
            return []
        results = []
        for _, r in df.iterrows():
            name = str(r.get("名称", ""))
            if "比特币" in name or "bitcoin" in name.lower():
                results.append({
                    "name": "比特币",
                    "value": _safe_float(r.get("最新价")),
                    "change_pct": _safe_float(r.get("涨跌幅")),
                    "group": "crypto",
                })
                break
        logger.debug(f"加密货币: {len(results)} 条")
        return results
    except Exception as e:
        logger.error(f"加密货币采集失败: {e}")
        return []
