"""
慧盘 · Source层 · 指数行情
Sina优先，Tencent降级，上层无感知。

使用:
    from sources.index import fetch_indices

    # 三大指数
    data = fetch_indices()
    # data = {'sh000001': {'name':'上证指数', 'price':3250.12, 'change_pct':-1.23, 'amount':3500.5}, ...}

    # 自定义指数
    data = fetch_indices(['sh000300', 'sh000905', 'sh000852', 'sz399303'])
"""

import requests

# ── 默认指数 ──────────────────────────────────
DEFAULT_CODES = ["sh000001", "sz399001", "sz399006"]

# 所有已知指数的中文名
INDEX_NAMES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sz399303": "国证2000",
    "sh000688": "科创50",
}

HTTP_TIMEOUT = 10


# ── Sina ──────────────────────────────────────

def _parse_sina(codes: list[str]) -> dict:
    """从Sina hq.sinajs.cn批量获取指数行情

    Returns:
        {code: {name, price, change_pct, amount(亿元)}}
    Raises:
        Exception: 接口被限制或返回空数据
    """
    codes_str = ",".join(codes)
    url = f"http://hq.sinajs.cn/list={codes_str}"
    headers = {"Referer": "https://finance.sina.com.cn"}

    resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    if "Forbidden" in resp.text or "Service not valid" in resp.text:
        raise Exception("Sina接口被限制")

    lines = [l for l in resp.text.strip().split("\n") if l.strip()]
    result = {}

    for line in lines:
        parts = line.split("=")
        if len(parts) < 2:
            continue
        code = parts[0].split("_")[-1].strip()
        vals = parts[1].strip('";\\r').split(",")

        if len(vals) > 9 and code in codes:
            prev_close = float(vals[2])
            price = float(vals[3])
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
            # vals[9] = 成交额（元），转亿
            try:
                amount = round(float(vals[9]) / 1e8, 2)
            except (ValueError, IndexError):
                amount = 0

            result[code] = {
                "name": INDEX_NAMES.get(code, code),
                "price": round(price, 2),
                "change_pct": change_pct,
                "amount": amount,
            }

    if not result:
        raise Exception("Sina返回空数据")
    return result


# ── Tencent ───────────────────────────────────

def _parse_tencent(codes: list[str]) -> dict:
    """从Tencent qt.gtimg.cn批量获取指数行情

    Returns:
        {code: {name, price, change_pct, amount(亿元)}}
    Raises:
        Exception: 返回空数据
    """
    codes_str = ",".join(codes)
    url = f"https://qt.gtimg.cn/q={codes_str}"

    resp = requests.get(url, timeout=HTTP_TIMEOUT)
    resp.encoding = "gbk"

    lines = [l for l in resp.text.strip().split("\n") if l.strip()]
    result = {}

    for line in lines:
        parts = line.split("=")
        if len(parts) < 2:
            continue
        code = parts[0].split("_")[-1].strip()
        vals = parts[1].strip('";\\r').split("~")

        if len(vals) > 37 and code in codes:
            price = float(vals[3])
            change_pct = round(float(vals[32]), 2)
            # vals[37] = 成交额（万元），转亿
            try:
                amount = round(float(vals[37]) / 1e4, 2)
            except (ValueError, IndexError):
                amount = 0

            result[code] = {
                "name": INDEX_NAMES.get(code, vals[1] if len(vals) > 1 else code),
                "price": round(price, 2),
                "change_pct": change_pct,
                "amount": amount,
            }

    if not result:
        raise Exception("Tencent返回空数据")
    return result


# ── 统一入口 ──────────────────────────────────

def fetch_indices(codes: list[str] = None) -> dict:
    """获取指数行情（Sina优先，Tencent降级）

    Args:
        codes: 指数代码列表，如 ['sh000001', 'sz399001']
               默认三大指数

    Returns:
        {
            'sh000001': {
                'name': '上证指数',
                'price': 3250.12,
                'change_pct': -1.23,
                'amount': 3500.5,    # 亿元
            },
            ...
        }
        全部失败返回空dict {}
    """
    if codes is None:
        codes = DEFAULT_CODES

    # Sina优先
    try:
        result = _parse_sina(codes)
        return result
    except Exception as e:
        print(f"  ⚠️ Sina指数失败: {e}，尝试Tencent...")

    # Tencent降级
    try:
        result = _parse_tencent(codes)
        print(f"  ✅ 指数获取成功 (Tencent)")
        return result
    except Exception as e2:
        print(f"  ❌ 指数获取全部失败: {e2}")

    return {}
