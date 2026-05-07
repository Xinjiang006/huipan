"""
watchlist_tracker.py · 猎手（趋势池 + 板块池）后端计算引擎
v4.0.1 · 2026-03-22

职责:
  1. 读 config/watchlist.json
  2. 获取美股行情（趋势池，Sina/腾讯 per-ticker source）
  3. 从 pkl 归档计算 A股回调指标（趋势池 + 板块池）
  4. 从新闻JSON统计关键词热度（趋势池）
  5. 判定四级信号 (entry/approaching/watching/weakening)
  6. 输出 static/data/watchlist_status.json

调度:
  05:00  run_watchlist_tracker(us_only=True)   美股端+新闻
  15:10  run_watchlist_tracker(us_only=False)   全量
"""

import json
import os
import pickle
import re
import statistics
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "watchlist.json"
STATIC_DATA = BASE_DIR / "static" / "data"
ARCHIVE_SPOT = STATIC_DATA / "archive" / "spot"
SPOT_CACHE = BASE_DIR / ".spot_cache.pkl"
US_PRICE_CACHE = STATIC_DATA / "us_prices_cache.json"
OUTPUT_PATH = STATIC_DATA / "watchlist_status.json"
NEWS_PATH = STATIC_DATA / "news.json"
INVESTING_NEWS_PATH = STATIC_DATA / "investing_news.json"
REGIME_HISTORY_PATH = STATIC_DATA / "regime_history.json"

US_CACHE_MAX_DAYS = 60  # 保留最近60天价格


# ═══════════════════════════════════════════════════════════════════════════
# 1. 读配置
# ═══════════════════════════════════════════════════════════════════════════

def load_watchlist():
    """读 config/watchlist.json, 过滤 status=active"""
    if not CONFIG_PATH.exists():
        logger.warning("watchlist.json 不存在, 返回空配置")
        return {"trends": [], "sectors": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["trends"] = [t for t in data.get("trends", []) if t.get("status") == "active"]
    data["sectors"] = [s for s in data.get("sectors", []) if s.get("status") == "active"]
    logger.info(f"加载watchlist: {len(data['trends'])}条趋势, {len(data['sectors'])}个板块")
    return data


# ═══════════════════════════════════════════════════════════════════════════
# 2. 美股行情
# ═══════════════════════════════════════════════════════════════════════════

def _http_get(url, encoding="gbk", timeout=10):
    """通用HTTP GET, 返回解码后的文本"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode(encoding, errors="replace")
            if "Forbidden" in text or len(text.strip()) < 10:
                return None
            return text
    except Exception as e:
        logger.warning(f"HTTP GET 失败 {url}: {e}")
        return None


def _parse_sina_us(code, text):
    """
    解析Sina美股行情
    var hq_str_gb_mu="美光科技,...,最新价,...";
    字段(逗号分隔): [0]名称 [1]当前价 [2]涨跌额 [6]前收 [7]开盘
    [12]52周高 [13]52周低 [25]成交量
    注: Sina美股字段顺序可能变化, 这里取关键字段
    """
    pattern = rf'var hq_str_{code}="(.+?)";'
    m = re.search(pattern, text)
    if not m:
        return None
    parts = m.group(1).split(",")
    if len(parts) < 26:
        return None
    try:
        return {
            "name": parts[0],
            "price": float(parts[1]) if parts[1] else 0,
            "change": float(parts[2]) if parts[2] else 0,
            "prev_close": float(parts[6]) if parts[6] else 0,
            "high_52w": float(parts[12]) if parts[12] else 0,
            "low_52w": float(parts[13]) if parts[13] else 0,
            "volume": int(float(parts[25])) if parts[25] else 0,
        }
    except (ValueError, IndexError):
        return None


def _parse_tencent_us(code, text):
    """
    解析腾讯美股行情
    v_usXXX="分隔符~字段...";
    字段[3]=当前价  字段[32]=涨跌幅%  字段[33]=最高  字段[34]=最低
    """
    ticker = code.replace("gb_", "").upper()
    pattern = rf'v_us{ticker}="(.+?)";'
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    parts = m.group(1).split("~")
    if len(parts) < 35:
        return None
    try:
        return {
            "name": parts[1] if len(parts) > 1 else ticker,
            "price": float(parts[3]) if parts[3] else 0,
            "change": 0,
            "prev_close": float(parts[4]) if parts[4] else 0,
            "high_52w": 0,
            "low_52w": 0,
            "volume": int(float(parts[6])) if parts[6] else 0,
        }
    except (ValueError, IndexError):
        return None


def fetch_us_quotes(symbols):
    """
    获取美股行情, 按 source 分发请求
    symbols: [{"code": "gb_mu", "name": "Micron", "source": "sina"}, ...]
    返回: {code: {price, change, ...}} 或 {code: None}
    """
    results = {}
    # 按source分组批量请求
    sina_codes = [s["code"] for s in symbols if s.get("source", "sina") == "sina"]
    tencent_codes = [s["code"] for s in symbols if s.get("source") == "tencent"]

    # Sina 批量
    if sina_codes:
        url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
        text = _http_get(url)
        if text:
            for code in sina_codes:
                parsed = _parse_sina_us(code, text)
                results[code] = parsed
        else:
            # Sina失败, 逐个fallback到腾讯
            logger.warning("Sina美股批量失败, fallback腾讯")
            for code in sina_codes:
                ticker = code.replace("gb_", "").upper()
                fb_url = f"https://qt.gtimg.cn/q=us{ticker}"
                fb_text = _http_get(fb_url)
                results[code] = _parse_tencent_us(code, fb_text) if fb_text else None

    # 腾讯 批量
    if tencent_codes:
        tickers = [c.replace("gb_", "").upper() for c in tencent_codes]
        url = f"https://qt.gtimg.cn/q={','.join(['us' + t for t in tickers])}"
        text = _http_get(url)
        if text:
            for code in tencent_codes:
                parsed = _parse_tencent_us(code, text)
                results[code] = parsed
        else:
            for code in tencent_codes:
                results[code] = None

    # yahoo source: stub, v4.1实现
    for s in symbols:
        if s.get("source") == "yahoo":
            results[s["code"]] = None
            logger.info(f"Yahoo source {s['code']} 暂未实现")

    return results


def update_us_price_cache(us_quotes):
    """
    渐进积累美股价格历史
    每天追加一条, 保留最近US_CACHE_MAX_DAYS天
    """
    cache = {}
    if US_PRICE_CACHE.exists():
        try:
            with open(US_PRICE_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    today_str = datetime.now().strftime("%Y-%m-%d")
    updated = False

    for code, quote in us_quotes.items():
        if quote is None or quote["price"] <= 0:
            continue
        if code not in cache:
            cache[code] = []
        # 检查今天是否已有数据
        existing_dates = {p["date"] for p in cache[code]}
        if today_str not in existing_dates:
            cache[code].append({"date": today_str, "close": quote["price"]})
            updated = True
        # 只保留最近N天
        cache[code] = cache[code][-US_CACHE_MAX_DAYS:]

    if updated:
        tmp = str(US_PRICE_CACHE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, US_PRICE_CACHE)
        logger.info(f"美股价格缓存更新: {len(us_quotes)}只")

    return cache


def calc_us_nd_change(cache, code, n=5):
    """从缓存算近N日涨幅%"""
    prices = cache.get(code, [])
    if len(prices) < 2:
        return None
    if len(prices) < n:
        return None  # 数据不足N天，返回None而非0
    base_price = prices[-n]["close"] if len(prices) >= n else prices[0]["close"]
    if base_price <= 0:
        return None
    return round((prices[-1]["close"] - base_price) / base_price * 100, 2)


def calc_us_5d_change(cache, code):
    """兼容旧调用"""
    val = calc_us_nd_change(cache, code, 5)
    return val if val is not None else 0


def calc_us_trend_status(cache, code):
    """判断美股趋势状态"""
    prices = cache.get(code, [])
    if len(prices) < 5:
        return "insufficient_data"
    closes = [p["close"] for p in prices[-10:]]
    if len(closes) < 3:
        return "insufficient_data"
    # 简单判断: 最近3天均涨 = accelerating, 整体涨 = rising, 否则 flat/falling
    recent3 = closes[-3:]
    if all(recent3[i] > recent3[i - 1] for i in range(1, len(recent3))):
        return "accelerating"
    if closes[-1] > closes[0]:
        return "rising"
    if closes[-1] < closes[0]:
        return "falling"
    return "flat"


# ═══════════════════════════════════════════════════════════════════════════
# 3. A股回调指标 (从pkl归档)
# ═══════════════════════════════════════════════════════════════════════════

def _load_pkl(path):
    """加载pkl文件, 返回DataFrame或None"""
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        # pkl结构: {"time": float, "df": DataFrame}
        if isinstance(data, dict) and "df" in data:
            return data["df"]
        # 如果直接就是DataFrame
        if hasattr(data, "columns"):
            return data
        return None
    except Exception as e:
        logger.debug(f"pkl加载失败 {path}: {e}")
        return None


def _get_available_pkls(days=30):
    """获取最近N天的pkl文件路径列表(按日期排序)"""
    if not ARCHIVE_SPOT.exists():
        return []
    files = sorted(ARCHIVE_SPOT.glob("spot_*.pkl"))
    return files[-days:] if len(files) > days else files


def _get_stock_from_df(df, code):
    """从DataFrame中提取指定股票的数据行, 代码格式: bj920000/sh688498/sz001309"""
    if df is None or not hasattr(df, "columns"):
        return None
    if "代码" not in df.columns:
        return None
    # 尝试带前缀匹配: sh688498, sz001309, bj920000
    for prefix in ["sh", "sz", "bj"]:
        full_code = f"{prefix}{code}"
        matches = df[df["代码"] == full_code]
        if not matches.empty:
            return matches.iloc[0]
    # 也尝试不带前缀的直接匹配
    matches = df[df["代码"] == code]
    if not matches.empty:
        return matches.iloc[0]
    # endswith匹配(兜底)
    matches = df[df["代码"].str.endswith(code, na=False)]
    if not matches.empty:
        return matches.iloc[0]
    return None


def _get_price_and_volume(row):
    """从DataFrame行提取价格和成交额"""
    price = 0
    volume = 0
    for p_col in ["最新价", "收盘价", "close", "price"]:
        if p_col in row.index:
            try:
                val = row[p_col]
                if val is not None and str(val).strip() not in ("", "nan", "None"):
                    price = float(val)
                    break
            except (ValueError, TypeError):
                pass
    for v_col in ["成交额", "amount", "turnover"]:
        if v_col in row.index:
            try:
                val = row[v_col]
                if val is not None and str(val).strip() not in ("", "nan", "None"):
                    volume = float(val)
                    break
            except (ValueError, TypeError):
                pass
    return price, volume


def calc_pullback(code, days=30):
    """
    从pkl归档计算单只A股的回调指标
    返回: {price, high_price, high_date, pullback_pct, volume_shrink_pct} 或 None
    """
    pkl_files = _get_available_pkls(days)
    if not pkl_files:
        # 尝试当日的spot_cache
        if SPOT_CACHE.exists():
            df = _load_pkl(SPOT_CACHE)
            row = _get_stock_from_df(df, code)
            if row is not None:
                price, vol = _get_price_and_volume(row)
                return {
                    "price": price, "high_price": price,
                    "high_date": datetime.now().strftime("%Y-%m-%d"),
                    "pullback_pct": 0, "volume_shrink_pct": 0,
                }
        return None

    # 收集历史价格和成交额
    history = []  # [(date_str, price, volume)]
    for pkl_path in pkl_files:
        fname = pkl_path.stem  # spot_YYYYMMDD
        date_str = fname.replace("spot_", "")
        try:
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        except Exception:
            continue
        df = _load_pkl(pkl_path)
        row = _get_stock_from_df(df, code)
        if row is None:
            continue
        price, vol = _get_price_and_volume(row)
        if price > 0:
            history.append((date_str, price, vol))

    # 加上当日数据
    if SPOT_CACHE.exists():
        df = _load_pkl(SPOT_CACHE)
        row = _get_stock_from_df(df, code)
        if row is not None:
            price, vol = _get_price_and_volume(row)
            today_str = datetime.now().strftime("%Y-%m-%d")
            if price > 0 and (not history or history[-1][0] != today_str):
                history.append((today_str, price, vol))

    if not history:
        return None

    # 找高点
    high_idx = max(range(len(history)), key=lambda i: history[i][1])
    high_date, high_price, high_volume = history[high_idx]
    current_date, current_price, current_volume = history[-1]

    # 回调幅度
    pullback_pct = round((high_price - current_price) / high_price * 100, 2) if high_price > 0 else 0

    # 缩量程度(相对高点日)
    volume_shrink_pct = 0
    if high_volume > 0 and current_volume > 0:
        volume_shrink_pct = round((1 - current_volume / high_volume) * 100, 1)
        volume_shrink_pct = max(0, volume_shrink_pct)  # 不允许负值(放量)

    return {
        "price": current_price,
        "high_price": high_price,
        "high_date": high_date,
        "pullback_pct": pullback_pct,
        "volume_shrink_pct": volume_shrink_pct,
    }


def calc_a_share_changes(code):
    """
    从pkl归档计算A股1d/5d/20d涨跌幅
    返回: {change_1d_pct, change_5d_pct, change_20d_pct}
    """
    result = {"change_1d_pct": None, "change_5d_pct": None, "change_20d_pct": None}

    pkl_files = _get_available_pkls(30)
    # 加上当日spot_cache
    all_prices = []  # [(date_str, price)]

    for pkl_path in pkl_files:
        fname = pkl_path.stem
        date_str = fname.replace("spot_", "")
        try:
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        except Exception:
            continue
        df = _load_pkl(pkl_path)
        row = _get_stock_from_df(df, code)
        if row is None:
            continue
        price, _ = _get_price_and_volume(row)
        if price > 0:
            all_prices.append((date_str, price))

    # 加当日数据
    if SPOT_CACHE.exists():
        df = _load_pkl(SPOT_CACHE)
        row = _get_stock_from_df(df, code)
        if row is not None:
            price, _ = _get_price_and_volume(row)
            today_str = datetime.now().strftime("%Y-%m-%d")
            if price > 0 and (not all_prices or all_prices[-1][0] != today_str):
                all_prices.append((today_str, price))

    if len(all_prices) < 2:
        return result

    current_price = all_prices[-1][1]

    # 1d: 今天 vs 昨天
    if len(all_prices) >= 2:
        prev = all_prices[-2][1]
        if prev > 0:
            result["change_1d_pct"] = round((current_price - prev) / prev * 100, 2)

    # 5d: 今天 vs 5天前
    if len(all_prices) >= 5:
        prev = all_prices[-5][1]
        if prev > 0:
            result["change_5d_pct"] = round((current_price - prev) / prev * 100, 2)

    # 20d: 今天 vs 20天前
    if len(all_prices) >= 20:
        prev = all_prices[-20][1]
        if prev > 0:
            result["change_20d_pct"] = round((current_price - prev) / prev * 100, 2)

    return result


def calc_sector_metrics(codes, days=30):
    """
    计算板块组的聚合指标
    返回: {avg_pullback, avg_shrink, sigma, vs_market_excess, stocks: [...]}
    """
    stock_results = []
    for code in codes:
        pb = calc_pullback(code, days)
        if pb and pb["price"] > 0:
            chg = calc_a_share_changes(code)
            stock_results.append({"code": code, **pb, **chg})

    if not stock_results:
        return None

    # 组平均回调
    avg_pullback = round(
        sum(s["pullback_pct"] for s in stock_results) / len(stock_results), 2
    )
    avg_shrink = round(
        sum(s["volume_shrink_pct"] for s in stock_results) / len(stock_results), 1
    )

    # sigma: 组回调幅度 / 组近20日日涨跌幅标准差
    sigma = _calc_group_sigma(codes, avg_pullback, days)

    # 超额回调: 组回调 - 大盘同期回调
    market_pullback = _calc_market_pullback(days)
    vs_market_excess = round(avg_pullback - market_pullback, 2)

    return {
        "avg_pullback": avg_pullback,
        "avg_shrink": avg_shrink,
        "sigma": sigma,
        "vs_market_excess": vs_market_excess,
        "market_pullback": market_pullback,
        "stocks": stock_results,
    }


def _calc_group_sigma(codes, avg_pullback, days=30):
    """
    计算组sigma = 组平均回调 / 组近20日日均波动率标准差
    """
    pkl_files = _get_available_pkls(min(days, 20))
    if len(pkl_files) < 5:
        return None  # 数据不足

    # 收集组内股票每日涨跌幅
    daily_returns = []  # 每天的组均涨跌幅
    prev_prices = {}

    for pkl_path in pkl_files:
        df = _load_pkl(pkl_path)
        if df is None:
            continue
        day_prices = {}
        for code in codes:
            row = _get_stock_from_df(df, code)
            if row is not None:
                price, _ = _get_price_and_volume(row)
                if price > 0:
                    day_prices[code] = price

        if prev_prices and day_prices:
            returns = []
            for code in day_prices:
                if code in prev_prices and prev_prices[code] > 0:
                    ret = (day_prices[code] - prev_prices[code]) / prev_prices[code]
                    returns.append(ret)
            if returns:
                daily_returns.append(sum(returns) / len(returns))

        prev_prices = day_prices

    if len(daily_returns) < 5:
        return None

    stdev = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.01
    if stdev < 0.001:
        stdev = 0.001  # 防止除零

    sigma = round((avg_pullback / 100) / stdev, 2)
    return sigma


def _calc_market_pullback(days=30):
    """计算大盘(上证指数)同期回调幅度"""
    pkl_files = _get_available_pkls(days)
    if not pkl_files:
        return 0

    # 上证指数不在个股pkl里, 尝试从regime_history读取
    # 简化: 用sh000001的数据, 如果pkl里有的话
    # fallback: 返回0(不影响信号判定, 只是超额=回调本身)
    prices = []
    for pkl_path in pkl_files:
        df = _load_pkl(pkl_path)
        if df is None:
            continue
        row = _get_stock_from_df(df, "000001")
        if row is not None:
            price, _ = _get_price_and_volume(row)
            if price > 0:
                prices.append(price)

    if len(prices) < 2:
        return 0

    high = max(prices)
    current = prices[-1]
    return round((high - current) / high * 100, 2) if high > 0 else 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. 新闻关键词热度
# ═══════════════════════════════════════════════════════════════════════════

def calc_news_heat(keywords):
    """
    统计关键词在新闻中的出现频次
    返回: {this_week: int, last_week: int, trend: "up"/"down"/"flat"}
    """
    all_articles = []

    for path in [NEWS_PATH, INVESTING_NEWS_PATH]:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 兼容不同的JSON结构
            if isinstance(data, list):
                all_articles.extend(data)
            elif isinstance(data, dict):
                for cat_articles in data.values():
                    if isinstance(cat_articles, list):
                        all_articles.extend(cat_articles)
        except Exception:
            continue

    now = datetime.now()
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)
    this_week = 0
    last_week = 0

    for article in all_articles:
        title = article.get("title", "") + " " + article.get("content", "")
        # 检查是否匹配任一关键词
        matched = any(kw.lower() in title.lower() for kw in keywords)
        if not matched:
            continue

        # 解析时间
        pub_time = article.get("time", "") or article.get("pub_date", "")
        try:
            if "T" in str(pub_time):
                pub_dt = datetime.fromisoformat(str(pub_time).replace("Z", ""))
            else:
                pub_dt = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        if pub_dt >= week_ago:
            this_week += 1
        elif pub_dt >= two_weeks_ago:
            last_week += 1

    if this_week > last_week:
        trend = "up"
    elif this_week < last_week:
        trend = "down"
    else:
        trend = "flat"

    return {"this_week": this_week, "last_week": last_week, "trend": trend}


# ═══════════════════════════════════════════════════════════════════════════
# 5. 信号判定
# ═══════════════════════════════════════════════════════════════════════════

def determine_trend_signal(trend, us_quotes, us_cache, news_heat):
    """
    趋势池信号判定
    - 逐只A股判定signal, 趋势级别取最强
    - weakening: 美股5日<0 且 新闻热度下降
    """
    a_signals = []
    entry_rule = trend.get("entry_rule", {})
    pb_threshold = entry_rule.get("pullback_pct", 12)
    vol_threshold = entry_rule.get("volume_shrink_pct", 30)

    for stock in trend.get("_a_data", []):
        pb = stock.get("pullback_pct", 0)
        vs = stock.get("volume_shrink_pct", 0)

        if pb >= pb_threshold and vs >= vol_threshold:
            sig = "entry"
        elif pb >= pb_threshold * 0.8 or (pb >= pb_threshold * 0.6 and vs >= vol_threshold * 0.8):
            sig = "approaching"
        else:
            sig = "watching"
        stock["signal"] = sig
        a_signals.append(sig)

    # 趋势级别 weakening 判定
    us_weak = False
    if trend.get("us_symbols"):
        us_5d_changes = []
        for sym in trend["us_symbols"]:
            chg = calc_us_5d_change(us_cache, sym["code"])
            us_5d_changes.append(chg)
        us_weak = all(c < 0 for c in us_5d_changes) if us_5d_changes else False

    news_down = news_heat.get("trend") == "down"

    # 取最强信号
    priority = {"entry": 0, "approaching": 1, "watching": 2, "weakening": 3}
    if a_signals:
        best = min(a_signals, key=lambda s: priority.get(s, 99))
    else:
        best = "watching"

    # weakening 覆盖: 美股弱+新闻降温, 且没有entry信号
    if us_weak and news_down and best not in ("entry",):
        best = "weakening"

    return best


def determine_sector_signal(metrics, entry_rule):
    """
    板块池信号判定
    四项条件: 回调% + 缩量% + sigma + 超额回调%
    全满足 → entry; 三项 → approaching; 否则 → watching
    weakening: 反弹>50% 或 sigma<0.5
    """
    if metrics is None:
        return "watching"

    conditions_met = 0
    pb = metrics["avg_pullback"]
    vs = metrics["avg_shrink"]
    sigma = metrics["sigma"]
    excess = metrics["vs_market_excess"]

    pb_threshold = entry_rule.get("pullback_pct", 10)
    vs_threshold = entry_rule.get("volume_shrink_pct", 30)
    sigma_threshold = entry_rule.get("min_sigma", 2.0)
    excess_threshold = entry_rule.get("vs_market_excess_pct", 5)

    if pb >= pb_threshold:
        conditions_met += 1
    if vs >= vs_threshold:
        conditions_met += 1
    if sigma is not None and sigma >= sigma_threshold:
        conditions_met += 1
    elif sigma is None:
        pass  # 数据不足, 不计入
    if excess >= excess_threshold:
        conditions_met += 1

    # weakening: sigma极低
    if sigma is not None and sigma < 0.5:
        return "weakening"

    if conditions_met >= 4:
        return "entry"
    elif conditions_met >= 3:
        return "approaching"
    else:
        return "watching"


def _assign_stock_signals(stocks, entry_rule):
    """给板块内每只股票分配signal"""
    pb_t = entry_rule.get("pullback_pct", 10)
    vs_t = entry_rule.get("volume_shrink_pct", 30)
    for s in stocks:
        if s["pullback_pct"] >= pb_t and s["volume_shrink_pct"] >= vs_t:
            s["signal"] = "entry"
        elif s["pullback_pct"] >= pb_t * 0.8:
            s["signal"] = "approaching"
        else:
            s["signal"] = "watching"


# ═══════════════════════════════════════════════════════════════════════════
# 6. regime环境
# ═══════════════════════════════════════════════════════════════════════════

def get_regime_env():
    """从regime_history.json读取最新regime信息"""
    default = {
        "label": "unknown", "label_cn": "未知",
        "momentum_winrate": 0, "reversion_winrate": 0,
    }
    if not REGIME_HISTORY_PATH.exists():
        return default
    try:
        with open(REGIME_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data:
            return default
        latest = data[0] if isinstance(data, list) else data
        label = latest.get("regime_label", "unknown")
        label_map = {
            "momentum": "追涨有效",
            "mean_reversion": "抄底有效",
            "reversion": "抄底有效",
            "rotation": "轮动",
            "choppy": "震荡",
        }
        mom_matched = latest.get("momentum_matched", 0) or 0
        mom_up = latest.get("momentum_up_count", 0) or 0
        rev_matched = latest.get("reversion_matched", 0) or 0
        rev_up = latest.get("reversion_up_count", 0) or 0
        mom_wr = (mom_up / mom_matched) if mom_matched > 0 else 0
        rev_wr = (rev_up / rev_matched) if rev_matched > 0 else 0
        return {
            "label": label,
            "label_cn": label_map.get(label, "未知"),
            "momentum_winrate": round(mom_wr * 100) if mom_wr < 1 else round(mom_wr),
            "reversion_winrate": round(rev_wr * 100) if rev_wr < 1 else round(rev_wr),
        }
    except Exception as e:
        logger.warning(f"读取regime_history失败: {e}")
        return default


# ═══════════════════════════════════════════════════════════════════════════
# 7. 主函数
# ═══════════════════════════════════════════════════════════════════════════

def _calc_days_tracked(added_str):
    """计算跟踪天数"""
    try:
        added = datetime.strptime(added_str, "%Y-%m-%d")
        return (datetime.now() - added).days
    except Exception:
        return 0


def build_status_json(us_only=False):
    """
    主函数: 拼装完整输出JSON
    us_only=True: 05:00跑, 只算美股端+新闻热度
    us_only=False: 15:10跑, 全量
    """
    watchlist = load_watchlist()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 美股行情 ──
    all_us_symbols = []
    for t in watchlist["trends"]:
        all_us_symbols.extend(t.get("us_symbols", []))
    us_quotes = fetch_us_quotes(all_us_symbols) if all_us_symbols else {}

    # ── 更新美股价格缓存 ──
    us_cache = update_us_price_cache(us_quotes)

    # ── 趋势池 ──
    trends_output = []
    for t in watchlist["trends"]:
        days_tracked = _calc_days_tracked(t.get("added", ""))

        # 美股端数据
        us_data = []
        for sym in t.get("us_symbols", []):
            quote = us_quotes.get(sym["code"])
            cached_prices = us_cache.get(sym["code"], [])

            # 实时价格; 无数据(周末/休市)时fallback缓存最后一条
            live_price = quote["price"] if quote and quote["price"] > 0 else 0
            if live_price <= 0 and cached_prices:
                live_price = cached_prices[-1]["close"]

            # 52周高点: 实时优先, 否则从缓存取最大值
            high_52w = quote.get("high_52w", 0) if quote else 0
            if high_52w <= 0 and cached_prices:
                high_52w = max(p["close"] for p in cached_prices)

            off_52w = 0
            if high_52w > 0 and live_price > 0:
                off_52w = round((live_price - high_52w) / high_52w * 100, 2)

            us_entry = {
                "code": sym["code"],
                "name": sym.get("name", sym["code"]),
                "price": live_price,
                "change_1d_pct": calc_us_nd_change(us_cache, sym["code"], 1),
                "change_5d_pct": calc_us_5d_change(us_cache, sym["code"]),
                "change_20d_pct": calc_us_nd_change(us_cache, sym["code"], 20),
                "off_52w_high_pct": off_52w,
                "trend_status": calc_us_trend_status(us_cache, sym["code"]),
                "prices_30d": [p["close"] for p in cached_prices[-30:]],
            }
            us_data.append(us_entry)

        # A股端数据 (us_only=True时跳过)
        a_data = []
        if not us_only:
            for sym in t.get("a_symbols", []):
                pb = calc_pullback(sym["code"])
                chg = calc_a_share_changes(sym["code"])
                if pb:
                    a_entry = {
                        "code": sym["code"],
                        "name": sym.get("name", sym["code"]),
                        **pb,
                        **chg,
                        "signal": "watching",
                        "entry_rule": t.get("entry_rule", {}),
                    }
                    a_data.append(a_entry)
                else:
                    a_data.append({
                        "code": sym["code"],
                        "name": sym.get("name", sym["code"]),
                        "price": 0, "high_price": 0, "high_date": "",
                        "pullback_pct": 0, "volume_shrink_pct": 0,
                        **chg,
                        "signal": "watching",
                        "entry_rule": t.get("entry_rule", {}),
                    })

        # 新闻热度
        news_heat = calc_news_heat(t.get("keywords", []))

        # 暂存A股数据到trend对象(供信号判定)
        t["_a_data"] = a_data

        # 信号判定
        signal = determine_trend_signal(t, us_quotes, us_cache, news_heat)
        signal_count = sum(1 for a in a_data if a.get("signal") == "entry")

        trends_output.append({
            "id": t["id"],
            "name": t["name"],
            "thesis": t.get("thesis", ""),
            "days_tracked": days_tracked,
            "signal": signal,
            "signal_count": signal_count,
            "us": us_data,
            "a": a_data,
            "news_heat": news_heat,
        })

    # ── 板块池 (us_only=True时跳过) ──
    sectors_output = []
    if not us_only:
        for s in watchlist["sectors"]:
            days_tracked = _calc_days_tracked(s.get("added", ""))
            metrics = calc_sector_metrics(s["codes"])
            entry_rule = s.get("entry_rule", {})
            signal = determine_sector_signal(metrics, entry_rule)

            sector_entry = {
                "id": s["id"],
                "name": s["name"],
                "days_tracked": days_tracked,
                "signal": signal,
                "signal_count": 0,
                "pullback_pct": 0,
                "volume_shrink_pct": 0,
                "sigma": None,
                "vs_market_excess_pct": 0,
                "stocks": [],
                "entry_rule": entry_rule,
            }

            if metrics:
                _assign_stock_signals(metrics["stocks"], entry_rule)
                sector_entry.update({
                    "signal_count": sum(
                        1 for st in metrics["stocks"] if st.get("signal") == "entry"
                    ),
                    "pullback_pct": metrics["avg_pullback"],
                    "volume_shrink_pct": metrics["avg_shrink"],
                    "sigma": metrics["sigma"],
                    "vs_market_excess_pct": metrics["vs_market_excess"],
                    "stocks": metrics["stocks"],
                })

            sectors_output.append(sector_entry)

    # ── regime环境 ──
    regime_env = get_regime_env()

    # ── 组装输出 ──
    output = {
        "updated_at": now_str,
        "regime_env": regime_env,
        "trends": trends_output,
        "sectors": sectors_output,
    }

    # ── 按信号优先级排序 ──
    signal_priority = {"entry": 0, "approaching": 1, "watching": 2, "weakening": 3}
    output["trends"].sort(key=lambda x: (
        signal_priority.get(x["signal"], 99), -x["days_tracked"]
    ))
    output["sectors"].sort(key=lambda x: (
        signal_priority.get(x["signal"], 99), -x["days_tracked"]
    ))

    # ── 写文件 ──
    STATIC_DATA.mkdir(parents=True, exist_ok=True)
    tmp = str(OUTPUT_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT_PATH)

    logger.info(
        f"watchlist_status.json 已生成: "
        f"{len(trends_output)}条趋势, {len(sectors_output)}个板块"
    )
    return output


# ═══════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════

def run_watchlist_tracker(us_only=False):
    """
    调度入口, 被 scheduler/jobs.py 调用
    us_only=True:  05:00, 只算美股端+新闻热度
    us_only=False: 15:10, 完整计算全部指标
    """
    mode = "美股端" if us_only else "全量"
    logger.info(f"=== 猎手追踪开始 ({mode}) ===")
    try:
        result = build_status_json(us_only=us_only)
        t_count = len(result.get("trends", []))
        s_count = len(result.get("sectors", []))
        # 统计信号
        entry_t = sum(1 for t in result["trends"] if t["signal"] == "entry")
        entry_s = sum(1 for s in result["sectors"] if s["signal"] == "entry")
        logger.info(
            f"=== 猎手追踪完成: {t_count}趋势({entry_t}入场) "
            f"{s_count}板块({entry_s}入场) ==="
        )
    except Exception as e:
        logger.error(f"猎手追踪失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    us_only = os.environ.get("HUIPAN_US_ONLY", "0") == "1"
    run_watchlist_tracker(us_only=us_only)
