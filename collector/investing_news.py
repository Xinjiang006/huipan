"""
慧盘 v3.2 · 英为财经中文新闻采集器
collector/investing_news.py

数据源：cn.investing.com RSS feeds
  - news.rss      最新财经资讯（综合）
  - news_25.rss   股票股市
  - news_14.rss   宏观与市场
  - news_11.rss   期货资讯

输出：static/data/investing_news.json
依赖：requests, feedparser
"""

import hashlib
import json
import os
import time
from datetime import datetime

import feedparser
import requests

# ── 配置 ──────────────────────────────────────

FEEDS = [
    {"url": "https://cn.investing.com/rss/news.rss",     "category": "综合"},
    {"url": "https://cn.investing.com/rss/news_25.rss",  "category": "股市"},
    {"url": "https://cn.investing.com/rss/news_14.rss",  "category": "宏观"},
    {"url": "https://cn.investing.com/rss/news_11.rss",  "category": "期货"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

REQUEST_TIMEOUT = 15
POLITE_DELAY = 1.0  # 请求间隔

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "investing_news.json")


def make_id(url: str) -> str:
    """URL去重用的短hash"""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def parse_pub_date(entry) -> str:
    """解析RSS条目的发布时间，返回 'YYYY-MM-DD HH:MM:SS' """
    # feedparser 会把 published_parsed 转成 time.struct_time
    tp = entry.get("published_parsed")
    if tp:
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", tp)
        except Exception:
            pass
    # fallback: 直接取 published 字符串
    raw = entry.get("published", "")
    if raw:
        return raw[:19]
    return ""


def fetch_feed(url: str, category: str) -> list[dict]:
    """抓取单个RSS feed，返回文章列表"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠ HTTP {r.status_code}: {url}")
            return []
        
        # 检查是否是有效的XML/RSS
        if "<?xml" not in r.text[:200] and "<rss" not in r.text[:200]:
            print(f"  ⚠ 不是RSS格式: {url}")
            return []
        
        feed = feedparser.parse(r.text)
        items = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            
            # 提取摘要（去HTML标签）
            summary = entry.get("summary", "")
            if summary:
                # 简单去标签
                import re
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                if len(summary) > 200:
                    summary = summary[:200] + "..."
            
            items.append({
                "id": make_id(link),
                "title": title,
                "summary": summary,
                "url": link,
                "category": category,
                "published": parse_pub_date(entry),
            })
        
        return items
    
    except Exception as e:
        print(f"  ❌ 采集失败 [{category}]: {e}")
        return []


def collect_investing_news(verbose: bool = True) -> dict:
    """
    采集所有feed，去重，按时间倒序排列
    返回 {"articles": [...], "fetched_at": "..."}
    """
    if verbose:
        print("=" * 60)
        print("慧盘 v3.2 · 英为财经中文新闻采集")
        print("=" * 60)
    
    all_items = []
    seen_ids = {}  # id → index in all_items
    
    for feed_cfg in FEEDS:
        url = feed_cfg["url"]
        category = feed_cfg["category"]
        if verbose:
            print(f"  📡 [{category}] {url}")
        
        items = fetch_feed(url, category)
        
        # 去重：相同文章合并分类
        new_count = 0
        for item in items:
            if item["id"] in seen_ids:
                # 已存在 → 追加分类
                idx = seen_ids[item["id"]]
                existing_cats = all_items[idx]["category"]
                if category not in existing_cats:
                    all_items[idx]["category"] += f",{category}"
            else:
                seen_ids[item["id"]] = len(all_items)
                all_items.append(item)
                new_count += 1
        
        if verbose:
            print(f"     → {len(items)}条，新增{new_count}条（去重后）")
        
        time.sleep(POLITE_DELAY)
    
    # 按发布时间倒序
    all_items.sort(key=lambda x: x.get("published", ""), reverse=True)
    
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if verbose:
        print(f"\n  ✅ 总计 {len(all_items)} 条（去重后）")
        print(f"  ⏰ {fetched_at}")
    
    return {
        "articles": all_items,
        "fetched_at": fetched_at,
    }


def save_json(data: dict, verbose: bool = True):
    """保存到 investing_news.json"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if verbose:
        print(f"  💾 已保存: {OUTPUT_FILE}")

    # --- DuckDB 入库 (v3.4) ---
    try:
        from storage.duckdb_v3_tables import DuckDBV3Store
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        store = DuckDBV3Store(os.path.join(BASE_DIR, "data", "huipan.duckdb"))
        store.save_investing_news(data.get('articles', []))
    except Exception as e:
        if verbose:
            print(f"  ⚠️ DuckDB入库跳过: {e}")


# ── 主入口 ──────────────────────────────────────

def main():
    data = collect_investing_news(verbose=True)
    save_json(data, verbose=True)


if __name__ == "__main__":
    main()
