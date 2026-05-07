"""
慧盘 v2 · 新闻采集器
collector/news.py

数据源（已验证可用）：
  - SCMP 南华早报（Economy / Business / China）
  - Yahoo Finance（首页 / 股市新闻 / 经济新闻）
  - WSJ RSS（Markets / World）
  - MarketWatch（首页 / 市场）
  - Barron's（首页）
  - AP Business

依赖：requests, beautifulsoup4, feedparser
"""

import hashlib
import time
import re
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
import feedparser


# ── 配置 ──────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT = 15
POLITE_DELAY = 1.5  # 同一站点请求间隔（秒）


# ── 关键词配置（从 config/keywords.json 加载）──────────────────

import json as _json

def _load_keywords(config_path: Optional[str] = None) -> tuple[list, list]:
    """
    从 JSON 配置文件加载关键词
    查找顺序：
      1. 指定路径
      2. config/keywords.json（相对于项目根目录）
      3. 同目录下的 ../config/keywords.json
    加载失败时使用内置默认值
    """
    import os

    search_paths = []
    if config_path:
        search_paths.append(config_path)

    # 项目根目录 = collector/ 的上一级
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    search_paths.append(os.path.join(project_root, "config", "keywords.json"))
    # 兜底：当前工作目录
    search_paths.append(os.path.join(os.getcwd(), "config", "keywords.json"))

    for path in search_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                t1 = [(item["kw"], item.get("wb", False)) for item in data.get("tier1", [])]
                t2 = [(item["kw"], item.get("wb", False)) for item in data.get("tier2", [])]
                print(f"  📋 关键词已加载: {path} (T1:{len(t1)} T2:{len(t2)})")
                return t1, t2
            except Exception as e:
                print(f"  ⚠️ 关键词配置加载失败 {path}: {e}")

    # 内置默认值（兜底）
    print("  ⚠️ 未找到 config/keywords.json，使用内置默认关键词")
    t1 = [
        ("china", False), ("chinese", False), ("beijing", False),
        ("hong kong", False), ("tariff", False), ("trade war", False),
        ("yuan", True), ("huawei", False), ("alibaba", False),
        ("tencent", False), ("tsmc", True), ("deepseek", False),
    ]
    t2 = [
        ("semiconductor", False), ("rare earth", False), ("lithium", False),
        ("oil price", False), ("crude oil", False), ("gold price", False),
        ("inflation", False), ("rate cut", False), ("nvidia", False),
        ("military", False), ("supply chain", False),
    ]
    return t1, t2

KEYWORDS_TIER1, KEYWORDS_TIER2 = _load_keywords()

# v3.8.1: 热加载支持 — 编辑keywords.json后无需restart容器
_kw_cache = {"mtime": 0, "t1": KEYWORDS_TIER1, "t2": KEYWORDS_TIER2}

def _get_keywords():
    """返回当前关键词，文件有变动时自动重新加载"""
    import os
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kw_path = os.path.join(project_root, "config", "keywords.json")
    try:
        mtime = os.path.getmtime(kw_path)
        if mtime > _kw_cache["mtime"]:
            t1, t2 = _load_keywords()
            _kw_cache["t1"], _kw_cache["t2"] = t1, t2
            _kw_cache["mtime"] = mtime
            print(f"  🔄 关键词已热加载（文件更新于 {datetime.fromtimestamp(mtime).strftime('%H:%M:%S')}）")
    except Exception:
        pass  # 文件不存在等异常，沿用缓存
    return _kw_cache["t1"], _kw_cache["t2"]


# ── 数据源定义 ──────────────────────────────────

SOURCES = {
    "scmp": {
        "name": "SCMP 南华早报",
        "type": "scrape",
        "urls": [
            "https://www.scmp.com/economy",
            "https://www.scmp.com/business",
            "https://www.scmp.com/news/china",
        ],
    },
    "yahoo": {
        "name": "Yahoo Finance",
        "type": "scrape",
        "urls": [
            "https://finance.yahoo.com/",
            "https://finance.yahoo.com/topic/stock-market-news/",
            "https://finance.yahoo.com/topic/economic-news/",
        ],
    },
    "wsj": {
        "name": "WSJ",
        "type": "rss",
        "urls": [
            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
            "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        ],
    },
    "marketwatch": {
        "name": "MarketWatch",
        "type": "scrape",
        "urls": [
            "https://www.marketwatch.com/",
            "https://www.marketwatch.com/markets",
        ],
    },
    "barrons": {
        "name": "Barron's",
        "type": "scrape",
        "urls": [
            "https://www.barrons.com/",
        ],
    },
    "ap": {
        "name": "AP Business",
        "type": "scrape",
        "urls": [
            "https://apnews.com/hub/business",
        ],
    },
}


# ── 工具函数 ──────────────────────────────────

def _make_id(source: str, url: str) -> str:
    raw = f"{source}:{url}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _keyword_in_text(keyword: str, word_boundary: bool, text_lower: str) -> bool:
    if word_boundary:
        return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text_lower))
    return keyword in text_lower


def _match_keywords(title: str) -> dict:
    t1, t2 = _get_keywords()  # v3.8.1: 热加载
    title_lower = title.lower()
    matched_t1 = [kw for kw, wb in t1 if _keyword_in_text(kw, wb, title_lower)]
    matched_t2 = [kw for kw, wb in t2 if _keyword_in_text(kw, wb, title_lower)]

    if matched_t1:
        return {"matched": True, "tier": 1, "keywords": matched_t1}
    elif matched_t2:
        return {"matched": True, "tier": 2, "keywords": matched_t2}
    return {"matched": False, "tier": 0, "keywords": []}


def _deduplicate(items: list[dict]) -> list[dict]:
    seen_titles = set()
    seen_urls = set()
    unique = []
    for item in items:
        norm = re.sub(r'\s+', ' ', item["title_en"].lower().strip())
        if len(norm) < 15:
            continue
        if norm in seen_titles:
            continue
        if item.get("url") and item["url"] in seen_urls:
            continue
        seen_titles.add(norm)
        if item.get("url"):
            seen_urls.add(item["url"])
        unique.append(item)
    return unique


def _fetch(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 429:
            time.sleep(3)
            resp2 = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp2.status_code == 200:
                return resp2.text
        print(f"  ⚠️ {url} → HTTP {resp.status_code}")
        return None
    except requests.exceptions.Timeout:
        print(f"  ❌ {url} → 超时")
    except requests.exceptions.ConnectionError:
        print(f"  ❌ {url} → 连接失败")
    except Exception as e:
        print(f"  ❌ {url} → {type(e).__name__}: {e}")
    return None


# ── 解析器 ──────────────────────────────────

def _parse_headings(html: str, source: str, base_url: str, page_url: str) -> list[dict]:
    """通用 h2/h3 标题解析（SCMP, Yahoo, MarketWatch, Barron's, AP）"""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for tag in soup.find_all(["h2", "h3"]):
        title = tag.get_text(strip=True)
        if not title or len(title) < 15:
            continue

        # 查找链接：优先级从外到内
        # 1. 祖先级 <a> 包裹 h2/h3（SCMP 结构）— 最可靠，一定是文章链接
        # 2. 父级就是 <a>
        # 3. h3 内嵌 <a>（Yahoo, MarketWatch）— 放最后，可能是分类链接
        href = ""
        ancestor_a = tag.find_parent("a")
        if ancestor_a and ancestor_a.get("href"):
            href = ancestor_a["href"]
        else:
            inner_a = tag.find("a")
            if inner_a and inner_a.get("href"):
                href = inner_a["href"]

        if href and href.startswith("/"):
            href = base_url + href

        items.append({
            "source": source,
            "title_en": title,
            "url": href,
        })
    return items


def _parse_rss(text: str, source: str) -> list[dict]:
    """RSS feed 解析"""
    feed = feedparser.parse(text)
    items = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "")
        if not title or len(title) < 15:
            continue
        items.append({
            "source": source,
            "title_en": title,
            "url": link,
        })
    return items


# 每个源的解析配置
PARSE_CONFIG = {
    "scmp":        ("scrape", "SCMP",        "https://www.scmp.com"),
    "yahoo":       ("scrape", "YAHOO",       "https://finance.yahoo.com"),
    "marketwatch": ("scrape", "MARKETWATCH", "https://www.marketwatch.com"),
    "barrons":     ("scrape", "BARRONS",     "https://www.barrons.com"),
    "ap":          ("scrape", "AP",          "https://apnews.com"),
    "wsj":         ("rss",    "WSJ",         ""),
}


# ── 主入口 ──────────────────────────────────

def collect_news(
    sources: Optional[list[str]] = None,
    filter_keywords: bool = True,
    verbose: bool = True,
) -> list[dict]:
    """
    采集新闻主入口

    参数:
        sources: 来源列表，None=全部
        filter_keywords: 是否按关键词过滤
        verbose: 是否打印过程

    返回:
        [{
            "id": "a1b2c3d4e5f6",
            "source": "SCMP",
            "title_en": "...",
            "url": "https://...",
            "tier": 1,
            "keywords": ["china", "tariff"],
            "fetched_at": "2026-03-09T07:00:00Z",
        }]
    """
    if sources is None:
        sources = list(SOURCES.keys())

    all_items = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for src_key in sources:
        src = SOURCES.get(src_key)
        if not src:
            continue
        cfg = PARSE_CONFIG.get(src_key)
        if not cfg:
            continue

        parse_type, label, base_url = cfg

        if verbose:
            print(f"\n📡 {src['name']}")

        for url in src["urls"]:
            text = _fetch(url)
            if not text:
                continue

            if parse_type == "rss":
                items = _parse_rss(text, label)
            else:
                items = _parse_headings(text, label, base_url, url)

            if verbose:
                print(f"  {url} → {len(items)} 条")
            all_items.extend(items)
            time.sleep(POLITE_DELAY)

    # 去重
    unique = _deduplicate(all_items)
    if verbose:
        print(f"\n📊 总计 {len(all_items)} 条 → 去重后 {len(unique)} 条")

    # 关键词过滤
    results = []
    for item in unique:
        match = _match_keywords(item["title_en"])
        if filter_keywords and not match["matched"]:
            continue

        item["id"] = _make_id(item["source"], item.get("url", item["title_en"]))
        item["tier"] = match["tier"]
        item["keywords"] = match["keywords"]
        item["fetched_at"] = now
        results.append(item)

    if verbose and filter_keywords:
        t1 = sum(1 for r in results if r["tier"] == 1)
        t2 = sum(1 for r in results if r["tier"] == 2)
        print(f"🎯 过滤后 {len(results)} 条（T1直接相关: {t1}, T2板块联动: {t2}）")

    return results


# ── CLI 测试 ──────────────────────────────────

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("慧盘 v2 · 新闻采集（正式版）")
    print("=" * 60)

    news = collect_news(filter_keywords=True)

    print("\n" + "=" * 60)

    if not news:
        print("⚠️ 无匹配结果，显示全量前20条：")
        all_news = collect_news(filter_keywords=False, verbose=False)
        for i, n in enumerate(all_news[:20]):
            print(f"  {i+1}. [{n['source']:10s}] {n['title_en'][:75]}")
    else:
        tier1 = [n for n in news if n["tier"] == 1]
        tier2 = [n for n in news if n["tier"] == 2]

        if tier1:
            print(f"\n🔴 T1 直接相关（{len(tier1)}条）:")
            for n in tier1:
                kw = ", ".join(n["keywords"][:3])
                print(f"  [{n['source']:10s}] {n['title_en'][:72]}")
                print(f"              ← {kw}")
        if tier2:
            print(f"\n🟡 T2 板块联动（{len(tier2)}条）:")
            for n in tier2:
                kw = ", ".join(n["keywords"][:3])
                print(f"  [{n['source']:10s}] {n['title_en'][:72]}")
                print(f"              ← {kw}")

    with open("news_output.json", "w", encoding="utf-8") as f:
        json.dump(news, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已写入 news_output.json（{len(news)}条）")
