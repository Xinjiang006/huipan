"""
慧盘 · Source层 · 同花顺数据
AKShare THS接口 + THS网页爬虫，上层无感知。

使用:
    from sources.ths import fetch_consecutive, fetch_new_highs_lows
    from sources.ths import fetch_sectors, fetch_etfs
"""

import os
import time
import requests
import pandas as pd
from io import StringIO


# ── 工具函数 ──────────────────────────────────

def _safe_float(val):
    """安全转换为float（兼容带%后缀的字符串）"""
    try:
        s = str(val).strip().rstrip('%')
        return round(float(s), 2)
    except (ValueError, TypeError):
        return None


def _extract_stock_details(df_page, type_key):
    """从THS网页表格提取个股明细（代码/名称/价格/涨跌幅）"""
    details = []
    cols = [str(c) for c in df_page.columns.tolist()]

    code_col = name_col = price_col = chg_col = None
    for i, c in enumerate(cols):
        if '代码' in c:
            code_col = i
        elif '名称' in c or '简称' in c:
            name_col = i
        elif '最新' in c or '现价' in c:
            price_col = i
        elif '涨跌幅' in c:
            chg_col = i

    if code_col is None: code_col = 1
    if name_col is None: name_col = 2
    if price_col is None: price_col = 3
    if chg_col is None: chg_col = 4

    for _, row in df_page.iterrows():
        try:
            shift = 1 if str(row.iloc[0]).strip() == '序号' else 0
            raw = str(row.iloc[code_col + shift]).strip().split('.')[0]
            if not raw.isdigit():
                continue
            code = raw.zfill(6)
            name = str(row.iloc[name_col + shift]).strip()
            chg = _safe_float(row.iloc[chg_col + shift])
            price = _safe_float(row.iloc[price_col + shift])
            details.append({
                "code": code,
                "name": name,
                "type": type_key,
                "change_pct": chg,
                "price": price,
            })
        except Exception:
            continue
    return details


# ── 连涨连跌 ──────────────────────────────────

def fetch_consecutive() -> dict:
    """THS连续上涨/下跌排行

    Returns:
        {
            'consecutive_up_3': int,    # 连涨≥3天的股票数
            'consecutive_down_3': int,  # 连跌≥3天的股票数
        }
    """
    import akshare as ak
    result = {}

    try:
        df = ak.stock_rank_lxsz_ths()
        col = next((c for c in df.columns if "连涨天数" in c or "连续涨跌天数" in c), None)
        count = int((df[col] >= 3).sum()) if col else 0
        result["consecutive_up_3"] = count
        print(f"  ✅ 连续上涨≥3天: {count}")
    except Exception as e:
        result["consecutive_up_3"] = 0
        print(f"  ❌ 连续上涨: {e}")

    time.sleep(0.5)

    try:
        df = ak.stock_rank_lxxd_ths()
        col = next((c for c in df.columns if "连跌天数" in c or "连涨天数" in c or "连续涨跌天数" in c), None)
        count = int((df[col] >= 3).sum()) if col else 0
        result["consecutive_down_3"] = count
        print(f"  ✅ 连续下跌≥3天: {count}")
    except Exception as e:
        result["consecutive_down_3"] = 0
        print(f"  ❌ 连续下跌: {e}")

    return result


# ── 新高新低 ──────────────────────────────────

def fetch_new_highs_lows() -> tuple:
    """从THS网页抓取创新高/新低数量（月/年/历史三周期）

    Returns:
        (kpi_dict, year_details)
        - kpi_dict: {'high_month': N, 'low_month': N, 'high_year': N, ...}
        - year_details: [{'code', 'name', 'type', 'change_pct', 'price'}, ...]
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.10jqka.com.cn/",
    }
    PERIODS = [
        ("high_month", "cxg", ""),
        ("low_month",  "cxd", ""),
        ("high_year",  "cxg", "board/2/"),
        ("low_year",   "cxd", "board/2/"),
        ("high_ath",   "cxg", "board/1/"),
        ("low_ath",    "cxd", "board/1/"),
    ]
    result = {k: 0 for k, _, _ in PERIODS}
    year_details = []

    label_map = {
        "high_month": "月新高", "low_month": "月新低",
        "high_year": "年新高", "low_year": "年新低",
        "high_ath": "历史新高", "low_ath": "历史新低",
    }

    for key, path, board in PERIODS:
        try:
            total = 0
            is_year = key in ("high_year", "low_year")
            prev_first_code = None
            for p in range(1, 30):
                url = f"https://data.10jqka.com.cn/rank/{path}/{board}page/{p}/free/1/"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    break
                tables = pd.read_html(StringIO(resp.text))
                if not tables:
                    break
                df_page = tables[0]
                rows = len(df_page)
                if rows == 0:
                    break
                first_row_str = " ".join(str(v) for v in df_page.iloc[0])
                if rows <= 1 and ("无符合" in first_row_str or "暂无" in first_row_str
                                  or first_row_str.replace(" ", "").replace("nan", "") == "无"):
                    break
                first_code = str(df_page.iloc[0, 1]) if rows > 0 else ""
                if first_code == prev_first_code:
                    break
                prev_first_code = first_code
                header_rows = sum(1 for _, r in df_page.iterrows()
                                 if not str(r.iloc[1]).strip().split('.')[0].isdigit())
                total += rows - header_rows

                if is_year and rows > 0:
                    year_details.extend(_extract_stock_details(df_page, key))

                if rows < 50:
                    break
                time.sleep(0.2)
            result[key] = total
            print(f"  ✅ {label_map[key]}: {total}只")
            time.sleep(0.3)
        except Exception as e:
            print(f"  ❌ {key}: {e}")

    return result, year_details


# ── 板块行情 ──────────────────────────────────

def fetch_sectors() -> tuple:
    """THS板块汇总

    Returns:
        (heatmap_sectors, all_sectors)
        - heatmap_sectors: 涨幅前7+跌幅前7（前端热力图用）
        - all_sectors: 全部~90个板块
        每项: {'name', 'change_pct', 'net_inflow', 'volume'}
    """
    import akshare as ak
    print("  获取板块数据...")
    try:
        df = ak.stock_board_industry_summary_ths()
        col_name = next((c for c in df.columns if "板块" in c), None)
        col_chg = next((c for c in df.columns if "涨跌幅" in c), None)
        col_flow = next((c for c in df.columns if "净流入" in c), None)
        col_vol = next((c for c in df.columns if "总成交额" in c), None)

        if not col_name or not col_chg:
            print("  ❌ 板块列名匹配失败")
            return [], []

        df[col_chg] = pd.to_numeric(df[col_chg], errors="coerce")
        df = df.dropna(subset=[col_chg])

        def row_to_dict(row):
            item = {"name": str(row[col_name])}
            item["change_pct"] = round(float(row[col_chg]), 2)
            if col_flow and pd.notna(row[col_flow]):
                item["net_inflow"] = round(float(row[col_flow]), 2)
            else:
                item["net_inflow"] = 0
            if col_vol and pd.notna(row[col_vol]):
                item["volume"] = round(float(row[col_vol]), 2)
            else:
                item["volume"] = 0
            return item

        all_sectors = [row_to_dict(row) for _, row in df.iterrows()]

        top = df.nlargest(7, col_chg)
        bottom = df.nsmallest(7, col_chg)
        combined = pd.concat([top, bottom]).drop_duplicates()
        heatmap_sectors = [row_to_dict(row) for _, row in combined.iterrows()]

        print(f"  ✅ 热力图{len(heatmap_sectors)}个, 全量{len(all_sectors)}个板块")
        return heatmap_sectors, all_sectors
    except Exception as e:
        print(f"  ❌ {e}")
        return [], []


# ── ETF行情 ──────────────────────────────────

def fetch_etfs() -> tuple:
    """全市场ETF行情：THS拿代码列表 → Sina批量查实时

    Returns:
        (etf_vol_top15, etf_chg_top_bottom)
        - etf_vol_top15: 成交额前15
        - etf_chg_top_bottom: 涨幅前8 + 跌幅前8
        每项: {'name', 'code', 'volume'(亿), 'change_pct'}
    """
    import akshare as ak
    print("  获取ETF数据（全市场）...")
    try:
        df = ak.fund_etf_spot_ths()
        code_col = next((c for c in df.columns if "基金代码" in c), df.columns[1])
        name_col = next((c for c in df.columns if "基金名称" in c), df.columns[2])

        sina_codes = []
        name_map = {}
        for _, row in df.iterrows():
            c = str(row[code_col]).zfill(6)
            n = str(row[name_col])
            if c.startswith(("51", "52", "56", "58")):
                sina_code = "sh" + c
            elif c.startswith(("15", "16")):
                sina_code = "sz" + c
            else:
                continue
            sina_codes.append(sina_code)
            name_map[sina_code] = n

        print(f"    同花顺{len(df)}只 → Sina格式{len(sina_codes)}只")

        etf_list = []
        sina_ok = False
        batch_size = 300
        try:
            for i in range(0, len(sina_codes), batch_size):
                batch = sina_codes[i:i + batch_size]
                codes_str = ",".join(batch)
                url = "http://hq.sinajs.cn/list=" + codes_str
                headers = {"Referer": "https://finance.sina.com.cn"}
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 403 or 'Forbidden' in resp.text:
                    raise ConnectionError("Sina 403 blocked")
                lines = [l for l in resp.text.strip().split("\n") if l.strip()]

                for line in lines:
                    try:
                        parts = line.split("=")
                        code = parts[0].split("_")[-1].strip()
                        vals = parts[1].strip('";\').split(",")
                        if len(vals) < 10 or not vals[3]:
                            continue
                        price = float(vals[3])
                        yesterday = float(vals[2])
                        if yesterday == 0 or price == 0:
                            continue
                        chg = round((price - yesterday) / yesterday * 100, 2)
                        vol_yi = round(float(vals[9]) / 1e8, 2)

                        display_name = name_map.get(code, vals[0])
                        short_code = code[2:] if len(code) > 2 else code

                        etf_list.append({
                            "name": display_name,
                            "code": short_code,
                            "volume": vol_yi,
                            "change_pct": chg,
                        })
                    except (ValueError, IndexError):
                        continue
            sina_ok = True
        except Exception as sina_err:
            print(f"    ⚠️ Sina blocked ({sina_err}), fallback to THS data")
            etf_list = []
            chg_col = next((c for c in df.columns if "增长率" in c), None)
            for _, row in df.iterrows():
                c = str(row[code_col]).zfill(6)
                n = str(row[name_col])
                try:
                    chg = float(row[chg_col]) if chg_col and row[chg_col] is not None else 0.0
                except (ValueError, TypeError):
                    chg = 0.0
                etf_list.append({
                    "name": n,
                    "code": c,
                    "volume": 0,
                    "change_pct": chg,
                })
            print(f"    THS fallback: {len(etf_list)}只ETF (无成交额)")

        etf_vol = sorted(etf_list, key=lambda x: x["volume"], reverse=True)[:15]
        sorted_by_chg = sorted(etf_list, key=lambda x: x["change_pct"], reverse=True)
        etf_chg = sorted_by_chg[:8] + sorted_by_chg[-8:]

        print(f"  ✅ {len(etf_list)}只ETF, 成交额榜{len(etf_vol)}, 涨跌幅{len(etf_chg)}")
        return etf_vol, etf_chg
    except Exception as e:
        print(f"  ❌ ETF: {e}")
        return [], []
