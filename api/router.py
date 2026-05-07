from __future__ import annotations
"""
慧盘 · FastAPI 对外接口
前端通过这里取所有数据，不直接访问 DuckDB

启动：uvicorn api.router:app --host 0.0.0.0 --port 8000
"""

import sys
sys.path.insert(0, ".")

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from datetime import date
from loguru import logger

import math

def clean_nan(obj):
    """递归把 NaN/Inf 替换成 None，避免 JSON 序列化报错"""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(i) for i in obj]
    return obj

from api.internal import (
    get_market_latest,
    get_market_history,
    get_sector_latest,
    get_etf_latest,
    get_northbound_latest,
    get_northbound_history,
    get_limit_up_latest,
    get_limit_up_consecutive,
)
from business.engine.percentile import MarketPercentileEngine
from business.engine.summary import get_market_summary
from business.engine.signals import run_all_signals

app = FastAPI(title="慧盘 API", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS：允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── 健康检查 ──────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "huipan-api"}


# ── 市场总览（前端首页核心接口）────────────────────────

@app.get("/api/market/overview")
def market_overview():
    """
    市场总览：今日核心指标 + 分位数结论 + 信号
    前端 Dashboard 主接口，一个请求拿全部数据
    """
    try:
        summary = get_market_summary(lookback_days=30)
        signals = run_all_signals()
        etf = get_etf_latest()
        north = get_northbound_latest()

        result = {
            "date": summary.get("date"),
            "sentiment_score": summary.get("sentiment_score"),
            "headline": summary.get("headline"),
            "conclusions": summary.get("conclusions", []),
            "signals": {
                "risk": signals.get("risk_signals", []),
                "opportunity": signals.get("opportunity_signals", []),
                "total": signals.get("total", 0),
            },
            "etf": etf.to_dict(orient="records") if etf is not None and not etf.empty else [],
            "northbound": north,
        }
        return clean_nan(result)
    except Exception as e:
        logger.error(f"market_overview error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── 市场情绪 ──────────────────────────────────────────

@app.get("/api/market/latest")
def market_latest():
    """今日市场情绪原始数据"""
    data = get_market_latest()
    if not data:
        raise HTTPException(status_code=404, detail="无数据")
    # date 对象序列化
    data["date"] = str(data["date"])
    return data


@app.get("/api/market/history")
def market_history(days: int = Query(default=30, ge=5, le=250)):
    """近 N 日市场情绪历史（用于前端画趋势图）"""
    df = get_market_history(days)
    if df.empty:
        return []
    df["date"] = df["date"].astype(str)
    return df.to_dict(orient="records")


@app.get("/api/market/percentile")
def market_percentile(days: int = Query(default=30, ge=5, le=250)):
    """当日各指标分位数结论"""
    today = get_market_latest()
    if not today:
        raise HTTPException(status_code=404, detail="无数据")
    engine = MarketPercentileEngine(lookback_days=days)
    return engine.get_all(today)


# ── 板块 ──────────────────────────────────────────────

@app.get("/api/sector/latest")
def sector_latest(top_n: int = Query(default=20, ge=5, le=100)):
    """最新板块资金流向排行"""
    df = get_sector_latest(top_n=top_n)
    if df.empty:
        return []
    df["date"] = df["date"].astype(str)
    return df.to_dict(orient="records")


# ── ETF ───────────────────────────────────────────────

@app.get("/api/etf/latest")
def etf_latest():
    """最新 ETF 快照（含份额变化）"""
    df = get_etf_latest()
    if df.empty:
        return []
    df["date"] = df["date"].astype(str)
    return df.to_dict(orient="records")


# ── 北向资金 ──────────────────────────────────────────

@app.get("/api/northbound/latest")
def northbound_latest():
    """最新北向资金数据"""
    data = get_northbound_latest()
    if not data:
        raise HTTPException(status_code=404, detail="无数据")
    data["date"] = str(data["date"])
    return clean_nan(data)


@app.get("/api/northbound/history")
def northbound_history(days: int = Query(default=30, ge=5, le=120)):
    """近 N 日北向资金历史"""
    df = get_northbound_history(days)
    if df.empty:
        return []
    df["date"] = df["date"].astype(str)
    return df.to_dict(orient="records")


# ── 涨停 ──────────────────────────────────────────────

@app.get("/api/limit_up/latest")
def limit_up_latest(limit: int = Query(default=50, ge=10, le=200)):
    """今日涨停股明细"""
    df = get_limit_up_latest(limit=limit)
    if df.empty:
        return []
    df["date"] = df["date"].astype(str)
    return df.to_dict(orient="records")


@app.get("/api/limit_up/consecutive")
def limit_up_consecutive(min_days: int = Query(default=2, ge=2, le=10)):
    """当前连续涨停 N 板以上的股票"""
    df = get_limit_up_consecutive(min_days=min_days)
    if df.empty:
        return []
    df["date"] = df["date"].astype(str)
    return df.to_dict(orient="records")


# ── 信号 ──────────────────────────────────────────────

@app.get("/api/signals")
def signals():
    """今日触发的所有信号"""
    return run_all_signals()

@app.get("/api/global/latest")
def global_latest():
    import akshare as ak
    result = {}
    
    # 港股
    try:
        df = ak.stock_hk_index_spot_sina()
        hsi = df[df['代码']=='HSI'].iloc[0]
        hstech = df[df['代码']=='HSTECH'].iloc[0]
        result['hsi'] = {'value': float(hsi['最新价']), 'chg': float(hsi['涨跌幅'])}
        result['hstech'] = {'value': float(hstech['最新价']), 'chg': float(hstech['涨跌幅'])}
    except: pass

    # 美股
    try:
        for sym, key in [('.DJI','dji'),('.IXIC','nasdaq'),('.INX','sp500')]:
            df = ak.index_us_stock_sina(symbol=sym)
            row = df.iloc[-1]
            prev = df.iloc[-2]['close'] if len(df)>1 else row['close']
            chg = (row['close']-prev)/prev*100 if prev else 0
            result[key] = {'value': float(row['close']), 'chg': round(chg,2)}
    except: pass

    # 国债
    try:
        from datetime import datetime, timedelta
        start = (datetime.now()-timedelta(days=10)).strftime('%Y%m%d')
        df = ak.bond_zh_us_rate(start_date=start)
        row = df.dropna(subset=['中国国债收益率10年']).iloc[-1]
        result['cn10y'] = float(row['中国国债收益率10年'])
        result['cn30y'] = float(row.get('中国国债收益率30年', 0))
        result['us10y'] = float(row['美国国债收益率10年'])
    except: pass

    # 黄金（SGE）
    try:
        df = ak.spot_hist_sge(symbol='Au99.99')
        result['gold_cny'] = float(df.iloc[-1]['close'])
    except: pass

    return clean_nan(result)
