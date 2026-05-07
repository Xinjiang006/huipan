"""
collector/te_commodities.py
抓取 Trading Economics 大宗商品数据（能源 + 金属）
输出：static/data/commodities.json
"""

import json
import re
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ── 配置 ──────────────────────────────────────────────
URL = "https://zh.tradingeconomics.com/commodities"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://zh.tradingeconomics.com/",
}

# 只保留这两个分类，其余跳过
KEEP_SECTIONS = {"能源", "金属", "指数", "工业"}

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "data", "commodities.json"
)


# ── 工具函数 ──────────────────────────────────────────
def parse_float(text: str) -> float | None:
    """把 '1.84%' / '-0.82' / '▲1.764' 等清洗成 float，失败返回 None"""
    if not text:
        return None
    cleaned = re.sub(r"[▲▼%,\s]", "", text.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_section_name(th) -> str:
    """取表头第一列的文字作为分类名"""
    return th.get_text(strip=True)


def parse_row(tr) -> dict | None:
    """解析一行 <tr>，返回 dict 或 None（跳过无效行）"""
    tds = tr.find_all("td")
    if len(tds) < 9:
        return None

    # 第0列：品种名 + 单位（单位通常在 <a> 后的小字或 <span>）
    td0 = tds[0]
    # 品种名：<b> 或第一个文字节点
    name_tag = td0.find("b") or td0.find("a")
    name = name_tag.get_text(strip=True) if name_tag else td0.get_text(strip=True).split("\n")[0].strip()

    # 单位：td0 里去掉 name 后剩余文本
    raw0 = td0.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in raw0.split("\n") if l.strip()]
    unit = lines[1] if len(lines) > 1 else ""

    if not name:
        return None

    # 第1列：物价
    price = parse_float(tds[1].get_text(strip=True))

    # 第2列：天变动（含 ▲/▼ 方向箭头）
    day_text = tds[2].get_text(strip=True)
    day_change = parse_float(day_text)

    # 第3列：%
    day_pct = parse_float(tds[3].get_text(strip=True))

    # 第4列：每周
    week_pct = parse_float(tds[4].get_text(strip=True))

    # 第5列：每月一次
    month_pct = parse_float(tds[5].get_text(strip=True))

    # 第6列：YTD
    ytd_pct = parse_float(tds[6].get_text(strip=True))

    # 第7列：YoY
    yoy_pct = parse_float(tds[7].get_text(strip=True))

    # 第8列：更新时间（"16:06" 或 "2026-03-12"）
    updated = tds[8].get_text(strip=True)

    return {
        "name": name,
        "unit": unit,
        "price": price,
        "day_change": day_change,
        "day_pct": day_pct,
        "week_pct": week_pct,
        "month_pct": month_pct,
        "ytd_pct": ytd_pct,
        "yoy_pct": yoy_pct,
        "updated": updated,
    }


# ── 主采集函数 ────────────────────────────────────────
def fetch_commodities() -> dict:
    print(f"[te_commodities] 开始抓取 {URL}")

    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    print(f"[te_commodities] 共发现 {len(tables)} 个表格")

    sections = []

    for table in tables:
        # 读分类名（第一列 th 文字）
        head_th = table.find("thead")
        if not head_th:
            continue
        first_th = head_th.find("th")
        if not first_th:
            continue
        section_name = parse_section_name(first_th)

        # 跳过不需要的分类
        if section_name not in KEEP_SECTIONS:
            print(f"[te_commodities] 跳过: {section_name}")
            continue

        print(f"[te_commodities] 解析: {section_name}")
        items = []

        tbody = table.find("tbody")
        if not tbody:
            continue

        for tr in tbody.find_all("tr"):
            try:
                row = parse_row(tr)
                if row:
                    items.append(row)
            except Exception as e:
                print(f"[te_commodities] 行解析异常: {e}")
                continue

        print(f"[te_commodities]   → {len(items)} 条")
        sections.append({"name": section_name, "items": items})

    result = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sections": sections,
    }
    return result


# ── 导出 JSON ─────────────────────────────────────────
def save(data: dict, path: str = OUTPUT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[te_commodities] 已写入 {path}")

    # --- DuckDB 入库 (v3.4) ---
    try:
        from storage.duckdb_v3_tables import DuckDBV3Store
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        store = DuckDBV3Store(os.path.join(BASE_DIR, "data", "huipan.duckdb"))
        store.save_commodities(datetime.now().strftime('%Y-%m-%d'), data.get('sections', []))
    except Exception as e:
        print(f"  ⚠️ DuckDB入库跳过: {e}")


# ── 入口 ──────────────────────────────────────────────
def run() -> dict:
    data = fetch_commodities()
    save(data)
    return data


if __name__ == "__main__":
    import pprint
    result = run()
    # 打印每个分类前3条，验证字段
    for sec in result["sections"]:
        print(f"\n=== {sec['name']} ({len(sec['items'])}条) ===")
        pprint.pprint(sec["items"][:3])
