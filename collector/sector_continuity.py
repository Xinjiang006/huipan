"""
sector_continuity.py  ·  慧盘 v4.6 (Enhanced)
板块流量分析：今日实时 Top100 vs 前 1/3/5 日对比
功能：补全了退出榜单个股 (dropped_stocks) 的今日实时涨幅，不再显示为 null。
"""

import json
import os
import pickle
import tempfile
import logging
import time
from datetime import datetime, date
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = BASE_DIR / 'static' / 'data'
ARCHIVE    = DATA_DIR / 'archive' / 'spot'
SPOT_CACHE = DATA_DIR / '.spot_cache.pkl'
PICKS_FILE = DATA_DIR / 'picks_history.json'
SECTOR_MAP = BASE_DIR / 'config' / 'sector_map.json'
OUT_FILE   = DATA_DIR / 'sector_continuity.json'


# ── 工具 ──────────────────────────────────────────────────────────────────────
def atomic_write(path: Path, data: dict):
    """原子写 JSON，防止前端读到半写文件"""
    tmp = Path(tempfile.mktemp(dir=path.parent, suffix='.tmp'))
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    tmp.replace(path)
    log.debug(f'atomic_write: {path.name}')


def load_spot_pkl(path: Path) -> pd.DataFrame | None:
    try:
        with open(path, 'rb') as f:
            raw = pickle.load(f)
        df = raw['df'] if isinstance(raw, dict) else raw
        return df if hasattr(df, 'columns') else None
    except Exception as e:
        log.warning(f'pkl 读取失败 {path}: {e}')
        return None


def load_sector_map() -> dict:
    code_to_sector: dict = {}
    # ① sector_map.json
    try:
        with open(SECTOR_MAP) as f:
            sm = json.load(f)
        for sector, codes in sm.get('map', {}).items():
            for code in codes:
                code_to_sector[str(code).zfill(6)] = sector
    except: pass

    # ② picks_history 补充
    try:
        with open(PICKS_FILE) as f:
            picks = json.load(f)
        for day_data in picks.values():
            for stock in day_data.get('top100_gainers', []) + day_data.get('top100_losers', []):
                code, sec = stock.get('code', ''), stock.get('sector', '')
                if code and sec and sec != '未知' and code not in code_to_sector:
                    code_to_sector[code] = sec
    except: pass
    return code_to_sector


def load_picks_history() -> dict:
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except: return {}


# ── 核心：获取数据（包含全量映射） ──────────────────────────────────────────────
def get_today_data(code_to_sector: dict) -> tuple[list[dict], dict]:
    """
    返回: (今日Top100列表, 今日全量代码->涨幅映射)
    """
    today_str = date.today().strftime('%Y%m%d')
    archive_path = ARCHIVE / f'spot_{today_str}.pkl'

    df = load_spot_pkl(SPOT_CACHE) if SPOT_CACHE.exists() else None
    if df is None and archive_path.exists():
        df = load_spot_pkl(archive_path)

    if df is None:
        return [], {}

    # 预处理
    df = df[~df['代码'].str.startswith('bj')].copy()
    df['_pct'] = pd.to_numeric(df['涨跌幅'], errors='coerce')
    df = df.dropna(subset=['_pct'])
    df['_code6'] = df['代码'].str[2:]

    # 生成全量字典 { '600000': 5.23, ... }
    full_today_dict = df.set_index('_code6')['_pct'].to_dict()

    # 生成 Top100
    top100_df = df.nlargest(100, '_pct')
    top100_list = []
    for _, row in top100_df.iterrows():
        code = row['_code6']
        top100_list.append({
            'code': code,
            'name': str(row.get('名称', '')),
            'change_pct': round(float(row['_pct']), 2),
            'sector': code_to_sector.get(code, '未知'),
        })

    return top100_list, full_today_dict


# ── 核心：对比计算 ────────────────────────────────────────────────────────────
def compute_period(today_stocks: list[dict],
                   ref_gainers: list[dict],
                   ref_losers: list[dict],
                   full_today_dict: dict) -> dict:
    
    today_codes = {s['code'] for s in today_stocks}
    ref_g_map   = {s['code']: s for s in ref_gainers}
    ref_l_map   = {s['code']: s for s in ref_losers}
    
    sectors: dict[str, dict] = {}

    # 初始化板块桶
    all_involved_sectors = set(s['sector'] for s in today_stocks if s['sector'] != '未知')
    all_involved_sectors.update(s.get('sector', '未知') for s in ref_gainers if s.get('sector', '未知') != '未知')
    
    for sec in all_involved_sectors:
        sectors[sec] = {'today': 0, 'ref': 0, 'cont': [], 'new_in': [], 'dropped': []}

    # 统计今日分布
    for s in today_stocks:
        if s['sector'] in sectors: sectors[s['sector']]['today'] += 1
    # 统计参考日分布
    for s in ref_gainers:
        sec = s.get('sector', '未知')
        if sec in sectors: sectors[sec]['ref'] += 1

    # 1. 遍历今日榜单：区分 cont 和 new_in
    for ts in today_stocks:
        code, sec = ts['code'], ts['sector']
        if sec not in sectors: continue

        ref_val = None
        is_from_loser = False
        
        if code in ref_g_map:
            ref_val = ref_g_map[code]['change_pct']
            sectors[sec]['cont'].append({
                'code': code, 'name': ts['name'],
                'ref_pct': ref_val, 'today_pct': ts['change_pct']
            })
        else:
            if code in ref_l_map:
                ref_val = ref_l_map[code]['change_pct']
                is_from_loser = True
            
            sectors[sec]['new_in'].append({
                'code': code, 'name': ts['name'],
                'ref_pct': ref_val, 'today_pct': ts['change_pct'],
                'from_loser': is_from_loser
            })

    # 2. 遍历参考日榜单：处理掉榜股 (dropped)
    for code, rs in ref_g_map.items():
        sec = rs.get('sector', '未知')
        if sec not in sectors or code in today_codes: continue

        # 核心修改：尝试获取今日真实涨幅
        raw_today_val = full_today_dict.get(code)
        today_val = round(float(raw_today_val), 2) if raw_today_val is not None else None

        sectors[sec]['dropped'].append({
            'code': code,
            'name': rs['name'],
            'ref_pct': rs['change_pct'],
            'today_pct': today_val # 这里现在有值了！
        })

    # 整理输出
    sector_out = {}
    for sec, d in sectors.items():
        sector_out[sec] = {
            'ref': d['ref'], 'today': d['today'], 'delta': d['today'] - d['ref'],
            'cont': len(d['cont']), 'new_in': len(d['new_in']), 'dropped': len(d['dropped']),
            'cont_stocks': sorted(d['cont'], key=lambda x: -x['today_pct']),
            'new_stocks': sorted(d['new_in'], key=lambda x: -x['today_pct']),
            'dropped_stocks': sorted(d['dropped'], key=lambda x: -(x['ref_pct'] or 0)),
        }

    sorted_by_delta = sorted(sector_out.items(), key=lambda x: -x[1]['delta'])
    return {
        'sectors': sector_out,
        'top_growing': [[s, d['delta']] for s, d in sorted_by_delta if d['delta'] > 0][:5],
        'top_fading': [[s, d['delta']] for s, d in sorted_by_delta if d['delta'] < 0][-5:],
    }


def run_sector_continuity():
    t_start = time.time()
    log.info('═══ sector_continuity 开始运行 ═══')
    code_to_sector = load_sector_map()
    picks = load_picks_history()

    # 获取今日数据
    today_stocks, full_today_dict = get_today_data(code_to_sector)
    if not today_stocks:
        log.warning('今日数据为空，跳过')
        return

    today_str = datetime.now().strftime('%Y-%m-%d')
    hist_dates = sorted([d for d in picks.keys() if d < today_str], reverse=True)

    output = {
        'date': today_str,
        'updated_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'today_count': len(today_stocks),
    }

    period_map = {'1d': 0, '3d': 2, '5d': 4}
    for period, idx in period_map.items():
        if idx >= len(hist_dates): continue
        
        ref_date = hist_dates[idx]
        ref_day = picks[ref_date]
        
        res = compute_period(today_stocks, ref_day.get('top100_gainers', []), 
                             ref_day.get('top100_losers', []), full_today_dict)
        
        res['ref_date'] = ref_date
        res['total_cont'] = sum(s['cont'] for s in res['sectors'].values())
        res['total_new'] = sum(s['new_in'] for s in res['sectors'].values())
        res['total_dropped'] = sum(s['dropped'] for s in res['sectors'].values())
        
        output[period] = res
        log.info(f'{period} ({ref_date}) 计算完成')

    atomic_write(OUT_FILE, output)
    log.info(f'═══ 完成 ({time.time() - t_start:.1f}s) ═══')


if __name__ == '__main__':
    run_sector_continuity()
