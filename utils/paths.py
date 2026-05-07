"""
慧盘 · 路径常量
v5.0 — 所有模块统一引用，不再各自硬编码
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 数据目录 ──
DATA_DIR = os.path.join(BASE_DIR, "static", "data")
DB_PATH = os.path.join(BASE_DIR, "data", "huipan.duckdb")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
ARCHIVE_SPOT_DIR = os.path.join(DATA_DIR, "archive", "spot")

# ── 缓存 ──
SPOT_CACHE = os.path.join(DATA_DIR, ".spot_cache.pkl")

# ── JSON输出 ──
REGIME_HISTORY = os.path.join(DATA_DIR, "regime_history.json")
REGIME_SNAPSHOT = os.path.join(DATA_DIR, "regime_snapshot.json")
SECTOR_REALTIME = os.path.join(DATA_DIR, "sector_realtime.json")
SECTOR_CONTINUITY = os.path.join(DATA_DIR, "sector_continuity.json")
ASHARE_OVERVIEW = os.path.join(DATA_DIR, "ashare_overview.json")
ASHARE_MOVERS = os.path.join(DATA_DIR, "ashare_movers.json")
DERIVED_INTRADAY = os.path.join(DATA_DIR, "derived_intraday.json")
NEW_HIGH_LOW = os.path.join(DATA_DIR, "new_high_low.json")
KPI_HISTORY = os.path.join(DATA_DIR, "kpi_history.json")
YESTERDAY_PICKS = os.path.join(DATA_DIR, "yesterday_picks.json")
PICKS_HISTORY = os.path.join(DATA_DIR, "picks_history.json")
INTRADAY_SNAPSHOT = os.path.join(DATA_DIR, "intraday_snapshot.json")
INTRADAY_HISTORY = os.path.join(DATA_DIR, "intraday_history.json")
WATCHLIST_STATUS = os.path.join(DATA_DIR, "watchlist_status.json")
TRANSITION_SCORECARD = os.path.join(DATA_DIR, "transition_scorecard.json")

# ── 配置 ──
SECTOR_MAP = os.path.join(CONFIG_DIR, "sector_map.json")
INDEX_CONSTITUENTS = os.path.join(CONFIG_DIR, "index_constituents.json")
WATCHLIST = os.path.join(CONFIG_DIR, "watchlist.json")
FEATURES = os.path.join(CONFIG_DIR, "features.json")
