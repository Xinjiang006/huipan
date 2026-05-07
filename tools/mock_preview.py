#!/usr/bin/env python3
"""
慧盘 · 前端Mock预览工具
─────────────────────────
用法：python3 tools/mock_preview.py [--port 8888] [--days 30]

功能：
  1. 在 static/ 同级生成 _preview/ 目录
  2. 复制 static/index.html 并改 DATA 路径为相对路径
  3. 生成 Tab4(暗流) + Tab5(脉动) 完整mock数据（N天）
  4. 生成 Tab1/2/3 最小桩（防JS报错）
  5. 启动 http.server，浏览器直接看效果

每次运行会覆盖旧 _preview/，改完前端后重跑即可。
"""

import json
import os
import sys
import shutil
import random
import math
import argparse
import http.server
import socketserver
from datetime import datetime, timedelta

# ─── 路径 ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")
PREVIEW_DIR = os.path.join(PROJECT_ROOT, "_preview")
PREVIEW_DATA = os.path.join(PREVIEW_DIR, "data")

# ─── 板块池 ───
SECTORS = [
    "电力设备", "医药生物", "电子", "计算机", "机械设备", "基础化工", "汽车",
    "食品饮料", "有色金属", "银行", "非银金融", "农林牧渔", "钢铁", "公用事业",
    "传媒", "通信", "国防军工", "房地产", "建筑材料", "社会服务", "商贸零售",
    "纺织服饰", "轻工制造", "环保", "石油石化", "煤炭", "家用电器", "建筑装饰",
    "纺织服饰", "美容护理", "综合",
]
REGIME_LABELS = ["momentum", "mean_reversion", "choppy", "rotating", "trending_up", "trending_down"]


def _r(lo, hi, dec=2):
    return round(random.uniform(lo, hi), dec)


def _ri(lo, hi):
    return random.randint(lo, hi)


def _split4(total):
    """随机拆分为4个非负整数，和=total"""
    cuts = sorted(random.sample(range(1, total), 3))
    return [cuts[0], cuts[1] - cuts[0], cuts[2] - cuts[1], total - cuts[2]]


def _split5(total):
    cuts = sorted(random.sample(range(1, total), 4))
    return [cuts[0], cuts[1] - cuts[0], cuts[2] - cuts[1], cuts[3] - cuts[2], total - cuts[3]]


# ══════════════════════════════════════
# Mock 生成器
# ══════════════════════════════════════

def gen_regime_history(days=30):
    """生成 regime_history.json（Tab4全部区域数据）"""
    base_date = datetime.now()
    history = []

    for i in range(days):
        d = (base_date - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        picks_d = (base_date - timedelta(days=days - i)).strftime("%Y-%m-%d")

        sh = _r(-2.5, 2.5)
        up_count = _ri(1500, 4000)
        down_count = 5400 - up_count
        lu = _ri(20, 120)
        ld = _ri(5, 40)
        mom_avg = _r(-3, 4)
        rev_avg = _r(-3, 3)

        # 市值/股价分布
        gc = _split4(100)
        lc = _split4(100)
        gp = _split5(100)
        lp = _split5(100)

        # 板块
        g_sectors = random.sample(SECTORS, _ri(10, 20))
        l_sectors = random.sample(SECTORS, _ri(10, 18))
        overlap = len(set(g_sectors[:5]) & set(l_sectors[:5]))
        sector_dist_g = {s: _ri(1, 15) for s in g_sectors[:_ri(8, 15)]}
        sector_dist_l = {s: _ri(1, 12) for s in l_sectors[:_ri(8, 12)]}

        # 分档T+1
        tiers = {}
        for pfx, base in [("mom", mom_avg), ("rev", rev_avg)]:
            for j, tier in enumerate(["micro", "small", "mid", "large"]):
                n = [_ri(50, 90), _ri(5, 25), _ri(2, 15), _ri(0, 5)][j]
                tiers[f"{pfx}_{tier}_avg"] = _r(base - 1, base + 1) if n else None
                tiers[f"{pfx}_{tier}_median"] = _r(base - 0.8, base + 0.8) if n else None
                tiers[f"{pfx}_{tier}_up"] = _ri(0, n) if n else 0
                tiers[f"{pfx}_{tier}_n"] = n

        # 衍生指标（区域3: 10个）
        hp_count = _ri(150, 250)
        derived = {
            "zt_premium_avg": _r(-3, 5), "cap_scissors": _r(-2, 2),
            "median_change_pct": _r(-2, 2), "volume_price_ratio": _r(0.5, 2.5),
            "change_pct_stdev": _r(1.5, 6, 3), "volume_concentration": _r(5, 18),
            "extreme_ratio": _r(0.3, 6), "high_price_count": hp_count,
            "high_price_avg_chg": _r(-2, 3, 3), "high_price_up_count": _ri(60, hp_count),
        }

        # 健康指标（区域4: 4个）
        health = {
            "breadth_5d_avg": _r(25, 75, 1),
            "zt_dt_ratio": round(lu / max(ld, 1), 2),
            "new_high_low_diff": _ri(-100, 300),
            "volatility_5d": _r(0.3, 2.0, 3),
        }

        # 前置分析（区域6: 24个）
        prior = {}
        for grp in ["gn", "ls"]:
            for win in [1, 3, 5]:
                prior[f"{grp}_prev{win}_same"] = _r(35, 80, 1)
                prior[f"{grp}_prev{win}_avg"] = _r(-5, 8) if grp == "gn" else _r(-8, 5)
                prior[f"{grp}_prev{win}_med"] = prior[f"{grp}_prev{win}_avg"] + _r(-1, 1)
                prior[f"{grp}_prev{win}_strong"] = _r(5, 40, 1)

        rec = {
            "date": d, "picks_date": picks_d,
            "volume_total": _r(8000, 15000, 0), "volume_rank_30d": _ri(1, 30),
            "limit_up": lu, "limit_down": ld,
            "up_count": up_count, "down_count": down_count,
            "up_ratio": round(up_count / (up_count + down_count) * 100, 1),
            "sh_change_pct": sh,
            "sz_change_pct": _r(sh - 0.5, sh + 0.5),
            "cyb_change_pct": _r(sh - 1, sh + 1),
            "csi1000_change_pct": _r(sh - 0.8, sh + 0.8),
            "momentum_avg_return": mom_avg, "momentum_median_return": _r(mom_avg - 0.5, mom_avg + 0.5),
            "momentum_up_count": _ri(20, 80), "momentum_matched": _ri(95, 100),
            "reversion_avg_return": rev_avg, "reversion_median_return": _r(rev_avg - 0.5, rev_avg + 0.5),
            "reversion_up_count": _ri(20, 70), "reversion_matched": _ri(95, 100),
            **tiers,
            "gainer_micro": gc[0], "gainer_small": gc[1], "gainer_mid": gc[2], "gainer_large": gc[3],
            "loser_micro": lc[0], "loser_small": lc[1], "loser_mid": lc[2], "loser_large": lc[3],
            "gainer_p0_10": gp[0], "gainer_p10_30": gp[1], "gainer_p30_50": gp[2],
            "gainer_p50_100": gp[3], "gainer_p100p": gp[4],
            "loser_p0_10": lp[0], "loser_p10_30": lp[1], "loser_p30_50": lp[2],
            "loser_p50_100": lp[3], "loser_p100p": lp[4],
            "sector_count_gainers": len(g_sectors), "sector_count_losers": len(l_sectors),
            "sector_overlap": overlap,
            "top_gainer_sectors": g_sectors[:5], "top_loser_sectors": l_sectors[:5],
            "sector_dist_gainers": sector_dist_g, "sector_dist_losers": sector_dist_l,
            "micro_cap_ratio_gainer": round(gc[0], 1),
            **derived, **health, **prior,
            "regime_label": random.choice(REGIME_LABELS),
            "fetched_at": d + "T15:10:00",
        }
        history.append(rec)

    history.reverse()  # 降序（最新在前）
    return history


def gen_new_high_low(days=30):
    """生成 new_high_low.json（区域7）"""
    base_date = datetime.now()

    nh_history = []
    for i in range(days):
        d = (base_date - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        nh_history.append({
            "date": d,
            "high_year_total": _ri(40, 120),
            "low_year_total": _ri(5, 40),
            "top_sectors": random.sample(SECTORS[:14], 3),
        })

    # 今日板块聚合
    today_high = []
    remaining = _ri(60, 100)
    for s in random.sample(SECTORS, 10):
        cnt = _ri(2, min(20, remaining))
        today_high.append({"sector": s, "count": cnt})
        remaining -= cnt
        if remaining <= 0:
            break
    today_high.sort(key=lambda x: -x["count"])

    today_low = []
    remaining = _ri(8, 25)
    for s in random.sample(SECTORS, 5):
        cnt = _ri(1, min(5, remaining))
        today_low.append({"sector": s, "count": cnt})
        remaining -= cnt
        if remaining <= 0:
            break
    today_low.sort(key=lambda x: -x["count"])

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": {
            "high_year": {
                "total": sum(s["count"] for s in today_high),
                "by_sector": today_high,
                "by_cap": {"微盘": _ri(15, 30), "小盘": _ri(10, 25), "中盘": _ri(10, 25), "大盘": _ri(5, 15)},
            },
            "low_year": {
                "total": sum(s["count"] for s in today_low),
                "by_sector": today_low,
                "by_cap": {"微盘": _ri(2, 8), "小盘": _ri(2, 6), "中盘": _ri(1, 4), "大盘": _ri(0, 3)},
            },
        },
        "history": nh_history,
    }


def gen_intraday_snapshot():
    """生成 intraday_snapshot.json（Tab5 脉动）"""
    slots = ["09:28", "10:30", "13:05", "14:30", "15:10"]
    snapshots = []
    for t in slots:
        decay = slots.index(t) * 0.15  # 越晚衰减越多
        snapshots.append({
            "time": t,
            "momentum": {
                "avg": _r(-1 + 2 - decay, 3 - decay),
                "median": _r(-1.5 + 1.5 - decay, 2 - decay),
                "up_count": _ri(30, 75),
                "matched": _ri(95, 100),
            },
            "reversion": {
                "avg": _r(-2, 1.5 - decay * 0.5),
                "median": _r(-1.5, 1 - decay * 0.5),
                "up_count": _ri(25, 60),
                "matched": _ri(95, 100),
            },
            "open_bull": {
                "avg": _r(0.5 - decay, 4 - decay),
                "median": _r(0.2 - decay, 3 - decay),
                "up_count": _ri(40, 80),
                "matched": 100,
            },
            "open_bear": {
                "avg": _r(-5, -0.5 - decay * 0.3),
                "median": _r(-4.5, -0.3 - decay * 0.3),
                "up_count": _ri(5, 35),
                "matched": 100,
            },
            "indices": {"sh": _r(-0.5, 1), "sz": _r(-0.3, 1.2), "cyb": _r(-0.8, 1.5)},
        })

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "picks_date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
        "opening_date": datetime.now().strftime("%Y-%m-%d"),
        "snapshots": snapshots,
    }


def gen_intraday_history(days=5):
    """生成 intraday_history.json（Tab5 5日对比表）"""
    base = datetime.now()
    history = []
    for i in range(days):
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        history.append({
            "date": d, "time": "15:10",
            "momentum": {"avg": _r(-2, 3), "median": _r(-2, 2), "up_count": _ri(30, 70), "matched": _ri(95, 100)},
            "reversion": {"avg": _r(-3, 2), "median": _r(-2, 1.5), "up_count": _ri(25, 60), "matched": _ri(95, 100)},
            "open_bull": {"avg": _r(-1, 5), "median": _r(-1, 4), "up_count": _ri(40, 85), "matched": 100},
            "open_bear": {"avg": _r(-6, -0.5), "median": _r(-5, -0.3), "up_count": _ri(5, 30), "matched": 100},
            "indices": {"sh": _r(-1.5, 1.5), "sz": _r(-1, 2), "cyb": _r(-2, 3)},
        })
    return history


def gen_stubs():
    """Tab1/2/3 最小桩数据（防JS报错，无实际内容）"""
    d = datetime.now().strftime("%Y-%m-%d")
    return {
        "ashare_overview.json": {"date": d, "kpi": {}, "cap_indices": {}, "sectors": [],
                                  "etf_vol": [], "etf_chg": [], "cap_distribution": {},
                                  "market_distribution": []},
        "ashare_movers.json": {"date": d, "gainers": [], "losers": [], "volume": [], "indices": {}},
        "hk_movers.json": {"date": d, "hk_gainers": [], "hk_losers": [], "hk_volume": [], "hk_hot": []},
        "us_movers.json": {"date": d, "gainers": [], "losers": []},
        "us_sectors.json": {"date": d, "sectors": []},
        "global_market.json": {"date": d, "items": []},
        "kpi_history.json": [],
        "commodities.json": {"date": d, "sections": []},
        "hot_rank.json": {"date": d},
        "news.json": {"articles": []},
        "investing_news.json": {"articles": []},
    }


# ══════════════════════════════════════
# 主流程
# ══════════════════════════════════════

def build(days=30):
    """生成所有mock数据到 _preview/"""

    # 清理并重建
    if os.path.exists(PREVIEW_DIR):
        shutil.rmtree(PREVIEW_DIR)
    os.makedirs(PREVIEW_DATA, exist_ok=True)

    # 复制 index.html 并改 DATA 路径
    if not os.path.exists(INDEX_PATH):
        print(f"❌ 找不到 {INDEX_PATH}")
        sys.exit(1)

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("const DATA = '/static/data/';", "const DATA = 'data/';")
    with open(os.path.join(PREVIEW_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✅ index.html → _preview/ (DATA路径已改)")

    # Tab4: regime_history.json
    regime = gen_regime_history(days)
    _write(regime, "regime_history.json")
    print(f"  ✅ regime_history.json: {len(regime)}天 × {len(regime[0])}字段")

    # Tab4: new_high_low.json
    nh = gen_new_high_low(days)
    _write(nh, "new_high_low.json")
    print(f"  ✅ new_high_low.json: {nh['today']['high_year']['total']}只新高, {len(nh['today']['high_year']['by_sector'])}板块")

    # Tab5: intraday
    snap = gen_intraday_snapshot()
    _write(snap, "intraday_snapshot.json")
    print(f"  ✅ intraday_snapshot.json: {len(snap['snapshots'])}个时间点")

    hist = gen_intraday_history(5)
    _write(hist, "intraday_history.json")
    print(f"  ✅ intraday_history.json: {len(hist)}天")

    # Tab1/2/3 桩
    stubs = gen_stubs()
    for fname, data in stubs.items():
        _write(data, fname)
    print(f"  ✅ Tab1/2/3 桩文件: {len(stubs)}个")


def _write(data, fname):
    path = os.path.join(PREVIEW_DATA, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def serve(port=8888):
    """启动 HTTP 服务器"""
    os.chdir(PREVIEW_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    handler.log_message = lambda *a: None  # 静默日志

    with socketserver.TCPServer(("", port), handler) as httpd:
        url = f"http://localhost:{port}"
        print(f"\n{'═' * 50}")
        print(f"  🚀 预览服务器已启动")
        print(f"  📎 {url}")
        print(f"  📎 {url}#regime  ← Tab4 暗流")
        print(f"  📎 {url}#intraday  ← Tab5 脉动")
        print(f"{'═' * 50}")
        print(f"  Ctrl+C 退出\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  👋 已关闭")


def main():
    parser = argparse.ArgumentParser(description="慧盘前端Mock预览工具")
    parser.add_argument("--port", type=int, default=8888, help="HTTP端口 (默认8888)")
    parser.add_argument("--days", type=int, default=30, help="Mock天数 (默认30)")
    parser.add_argument("--no-serve", action="store_true", help="只生成数据不启动服务器")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（固定数据复现）")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print(f"\n[mock_preview] 生成{args.days}天mock数据...")
    build(args.days)

    if not args.no_serve:
        serve(args.port)
    else:
        print(f"\n  数据已生成到 {PREVIEW_DIR}/")
        print(f"  手动启动: cd {PREVIEW_DIR} && python3 -m http.server {args.port}")


if __name__ == "__main__":
    main()
