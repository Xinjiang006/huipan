#!/bin/bash
# 慧盘 · 一键刷新脚本
# v3.5.1: 加入自动归档（spot/news/us/hk分目录）
#
# 执行顺序说明：
#   1. [周一] 更新板块映射表（sector_map.json）
#   2. A股 movers（内含：regime计算 → yesterday_picks保存）
#   3. A股 overview
#   4. 港股 + 美股 + 全球行情 + 新闻
#   5. 大宗商品
#   6. 雪球人气榜
#   7. investing_news
#   8. 归档当日数据（spot/news/us/hk）

cd ~/huipan && source .venv/bin/activate

TODAY=$(date '+%Y%m%d')
NOW=$(date '+%Y-%m-%d %H:%M:%S')
echo "🔄 ${NOW} 开始刷新..."

# ─── 1. 板块映射表（每周一更新）───
DAY_OF_WEEK=$(date '+%u')  # 1=周一, 7=周日
if [ "$DAY_OF_WEEK" = "1" ]; then
    echo "📋 周一：更新板块映射表..."
    python3 collector/update_sector_map.py
    echo "  板块映射表更新完成"
else
    echo "📋 今日非周一（${DAY_OF_WEEK}），跳过板块映射更新"
fi

# ─── 2. A股（movers先跑，overview复用pkl缓存）───
# movers末尾：先 regime计算（读昨日picks） → 再保存 yesterday_picks（供明日regime）
python3 collector/ashare_movers.py
python3 collector/ashare_overview.py

# ─── 3. 其他数据源 ───
python3 -c "
from storage.duckdb_v2_store import collect_and_save_news, collect_and_save_us_market, collect_and_save_global, collect_and_save_hk
collect_and_save_hk()
collect_and_save_us_market()
collect_and_save_global()
collect_and_save_news()
"

# ─── 4. 大宗商品 ───
python3 collector/te_commodities.py

# ─── 5. 雪球人气榜 ───
python3 collector/hot_rank.py

# ─── 6. investing_news ───
python3 -m collector.investing_news

# ─── 7. 归档当日数据 ───
echo "📦 归档当日数据..."
ARCHIVE=static/data/archive
mkdir -p ${ARCHIVE}/{spot,news,us,hk}

# spot pkl（回测核心，必须归档）
[ -f static/data/.spot_cache.pkl ] && \
    cp static/data/.spot_cache.pkl ${ARCHIVE}/spot/spot_${TODAY}.pkl && \
    echo "  ✅ spot → spot_${TODAY}.pkl"

# 新闻
[ -f static/data/news.json ] && \
    cp static/data/news.json ${ARCHIVE}/news/news_${TODAY}.json
[ -f static/data/investing_news.json ] && \
    cp static/data/investing_news.json ${ARCHIVE}/news/investing_news_${TODAY}.json

# 美股+全球
[ -f static/data/us_movers.json ] && \
    cp static/data/us_movers.json ${ARCHIVE}/us/us_movers_${TODAY}.json
[ -f static/data/us_sectors.json ] && \
    cp static/data/us_sectors.json ${ARCHIVE}/us/us_sectors_${TODAY}.json
[ -f static/data/global_market.json ] && \
    cp static/data/global_market.json ${ARCHIVE}/us/global_market_${TODAY}.json

# 港股
[ -f static/data/hk_movers.json ] && \
    cp static/data/hk_movers.json ${ARCHIVE}/hk/hk_movers_${TODAY}.json

echo "✅ $(date '+%Y-%m-%d %H:%M:%S') 刷新完成"
