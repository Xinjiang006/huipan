# 慧盘 v2 · 技术设计文档

> 版本：v3.1 · 2026-03-15  
> 仓库：https://github.com/Xinjiang006/huipan  
> 环境：阿里云 ECS · Ubuntu · Python 3.11 · 虚拟环境 `~/huipan/.venv`  
> 状态：**三Tab全部完成，v3.1细节优化，待ECS部署**

---

## 1. 产品方向

### 1.1 核心定位

从 v1 的市场仪表盘转向**跨市场信号系统**。

**展示原则**：同花顺告诉你发生了什么，慧盘告诉你这意味着什么。

### 1.2 分阶段规划

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 新闻采集 + 美股动量扫描 + 前端展示 | **✅ 前端完成，待部署** |
| Phase 2 | 跨市场映射表 + LLM关联推理 | 未开始 |
| Phase 3 | 历史回测 + 概率验证 | 未开始 |

---

## 2. 系统架构

### 2.1 部署架构（v2.4确认）

```
WSL 本地（数据采集+处理）
├── collector/*.py → AKShare + 爬虫 + Sina API
├── storage/duckdb_v2_store.py → DuckDB入库
├── data_export.py → JSON导出 → static/data/*.json
└── rsync/scp → 推送到ECS
    ↓
阿里云 ECS（纯静态服务）
├── Nginx → 端口80
├── /data/*.json → 数据文件
└── /index.html → 前端
```

**关键决策**：ECS不跑FastAPI/DuckDB，只做Nginx静态服务。

### 2.2 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 数据采集 | AKShare + requests + BeautifulSoup + feedparser | 多源 |
| 存储 | DuckDB | WSL本地，不部署到ECS |
| 前端 | 原生HTML/CSS/JS | 单文件，加载JSON |
| LLM | Deepseek V3 + Claude API | 新闻翻译+报告（待开发） |
| 部署 | Nginx | ECS静态服务 |
| 同步 | rsync/scp | WSL→ECS |

---

## 3. 数据采集层

文件位置：`~/huipan/collector/`

### 3.1 数据源策略

**彻底弃用东财（`_em`后缀）接口**，全部替换为 Sina + 同花顺：

| 数据 | 旧接口（东财，已弃用） | 新接口 | 说明 |
|------|------------------------|--------|------|
| 全市场行情 | `stock_zh_a_spot_em()` | `stock_zh_a_spot()` | Sina源，5488只，~40s |
| A股指数 | `stock_zh_index_spot_em()` | Sina批量 `hq.sinajs.cn` | 0.1s |
| 板块热力图 | `stock_sector_fund_flow_rank()` | `stock_board_industry_summary_ths()` | 同花顺，90板块 |
| ETF行情 | `fund_etf_spot_em()` | `fund_etf_spot_ths()` + Sina批量 | 同花顺+Sina |
| 涨停/跌停 | `stock_zt_pool_em()` | `stock_zh_a_spot()` 自算 | 按涨跌幅阈值判定 |
| 创新高/新低 | `stock_rank_cxg_ths()` (bug) | 同花顺网页分页遍历 | 月/年/历史 三周期（v2.9） |
| **港股** | `stock_hk_ggt_components_em()` | **`stock_hk_spot()`** + Sina hot_hk API | **v2.8迁移** |

**弃用原因**：东财接口封IP不稳定，`RemoteDisconnected` 错误频发。港股接口于v2.8确认失效并完成迁移。

### 3.2 采集器清单

| 采集器 | 状态 | 输出JSON | 说明 |
|--------|------|----------|------|
| `news.py` | ✅ | `news.json` | 6源新闻采集+关键词过滤 |
| `us_market.py` | ✅ | `us_movers.json` + `us_sectors.json` | Yahoo榜单(含volume_str)+Sina 21只ETF |
| `global_market.py` | ✅ | `global_market.json` | Sina 20项+AKShare国债3项=23项 |
| `hk_market.py` | ✅ v2.8重写 | `hk_movers.json` | 新浪全港股~2741只(过滤后~1000只)+热门港股API |
| `ashare_movers.py` | ✅ v2.6重写 | `ashare_movers.json` | Sina源全市场行情+涨跌榜+缓存 |
| `ashare_overview.py` | ✅ v3.1更新 | `ashare_overview.json` | 同花顺板块/ETF全市场+创新高新低（三周期）+涨跌分布+全市场分布+KPI快照 |
| `te_commodities.py` | ✅ v2.6 | `commodities.json` | Trading Economics大宗商品（4分类） |
| `hot_rank.py` | ✅ v3.1 | `hot_rank.json` | 雪球人气榜3类×2子榜（关注/讨论/交易×新增/热门） |

### 3.3 ashare_movers.py（v2.6重写）

**数据源**：
- `ak.stock_zh_a_spot()` — Sina源全市场行情（替代东财）
- Sina批量 `hq.sinajs.cn` — 上证/深证/创业板指数

**缓存机制**：采集后存 `static/data/.spot_cache.pkl`，供 ashare_overview.py 10分钟内复用（避免重复调用40s接口）。

**输出格式** `ashare_movers.json`：
```json
{
  "date": "2026-03-12",
  "fetched_at": "2026-03-12 15:50:00",
  "indices": {
    "sh":  {"name": "上证指数", "value": 4129.10, "change_pct": -0.10},
    "sz":  {"name": "深证成指", "value": 14374.87, "change_pct": -0.63},
    "cyb": {"name": "创业板指", "value": 3317.52, "change_pct": -0.96}
  },
  "gainers": [{"code":"600406","name":"国电南瑞","price":28.5,"change_pct":10.01}, ...],
  "losers":  [...],
  "volume":  [{"code":"300059","name":"东方财富","price":18.2,"change_pct":-9.98,"amount":142.0}, ...]
}
```

### 3.4 ashare_overview.py（v3.0更新）

**数据源（全部非东财）**：

| 数据 | 接口 | 说明 |
|------|------|------|
| 涨跌数/成交额/涨停跌停 | `stock_zh_a_spot()` Sina | 全市场统计+自算涨停 |
| 大中小微盘涨跌分布 | `stock_zh_a_spot()` Sina + `index_stock_cons()` | 按指数成分股4档×涨跌幅11桶（v3.0） |
| 连续上涨≥3 | `stock_rank_lxsz_ths()` | 同花顺 |
| 连续下跌≥3 | `stock_rank_lxxd_ths()` | 同花顺，列名"连涨天数" |
| 创新高/新低 | `data.10jqka.com.cn/rank/cxg\|cxd/{board}/page/N/free/1/` | 月(无board)/年(board/2/)/历史(board/1/) 三周期分页累加 |
| 大中小微盘 | Sina批量 | 沪深300/中证500/中证1000/国证2000 |
| 板块热力图 | `stock_board_industry_summary_ths()` | 涨幅前7+跌幅前7=14个 |
| ETF热点 | `fund_etf_spot_ths()` + Sina批量 | 全市场~1400只，成交额前15+涨跌幅各8 |

**容错机制**：每个数据源独立 `try/except`，单个失败不影响其他数据。

**KPI快照（v3.0新增）**：采集完成后自动将15个KPI字段存入DuckDB `kpi_daily_snapshot` 表（INSERT OR REPLACE by date），同时导出最近30条到 `kpi_history.json`。用于前端30日排名显示。

### 3.5 hk_market.py（v2.8重写）

**v2.8 迁移原因**：东财 `stock_hk_ggt_components_em()` 返回 `RemoteDisconnected`，与A股东财接口封IP问题一致。

**新数据源**：

| 源 | 接口 | 说明 |
|----|------|------|
| 全量港股 | `ak.stock_hk_spot()` | 新浪源，~2741只 |
| 热门港股 | Sina `Market_Center.getHKStockData?node=hot_hk` | ~20只，按成交额排序 |

**过滤规则**：最新价 < 0.1 HKD 或成交额 < 100万 HKD 的剔除（过滤仙股+低流动性），过滤后约1000只。

**输出格式** `hk_movers.json`：
```json
{
  "hk_gainers": [{"code":"00381","name":"权识国际","price":0.25,"change":0.09,"change_pct":55.41,"volume_hkd":0.1}, ...],
  "hk_losers": [...],
  "hk_volume": [...],
  "hk_hot": [{"code":"00700","name":"腾讯控股","price":547.50,"change":1.0,"change_pct":0.18,"volume_hkd":94.6}, ...],
  "total_count": 1001,
  "fetched_at": "2026-03-14T08:00:00Z"
}
```

**前端改动**："港股通"→"港股"，新增"热门"tab（共4个tab：涨幅/跌幅/成交额/热门）。

### 3.6 te_commodities.py（v2.6新增）

- 数据源：`zh.tradingeconomics.com/commodities`
- requests + BeautifulSoup，保留4个分类：能源、金属、工业、指数
- 输出 `commodities.json`，含 sections/items 结构
- 已验证无反爬

### 3.7 hot_rank.py（v3.1重写）

- 数据源：AKShare 雪球接口 `stock_hot_follow_xq` / `stock_hot_tweet_xq` / `stock_hot_deal_xq`
- 3类×2子榜×Top50：关注(新增/热门)、讨论(新增/热门)、交易(新增/热门)
- symbol参数：`"本周新增"` / `"最热门"`
- 输出 `hot_rank.json`，6个key：`follow_new/hot`, `tweet_new/hot`, `deal_new/hot`

---

## 4. JSON 文件清单

`static/data/` 目录下所有JSON文件：

| 文件 | 采集器 | 前端位置 | 说明 |
|------|--------|----------|------|
| `ashare_overview.json` | ashare_overview.py | Tab1 Col1 KPI + Col2 热力图/ETF | v3.1更新（+全市场涨跌分布） |
| `ashare_movers.json` | ashare_movers.py | Tab1 Col3 A股涨跌榜 | v2.6重写 |
| `hk_movers.json` | hk_market.py | Tab1 Col4 港股（含热门） | v2.8重写 |
| `us_movers.json` | us_market.py | Tab1 Col5 美股涨跌 | 含volume_str |
| `us_sectors.json` | us_market.py | Tab1 Col5 板块ETF tab | 已有 |
| `global_market.json` | global_market.py | Tab1 Col4/5 指数chip | 恒生解析已修复 |
| `news.json` | news.py | Tab3 英文新闻 | 48小时窗口 |
| `commodities.json` | te_commodities.py | Tab2 大宗商品 | v2.6新增 |
| `hot_rank.json` | hot_rank.py | Tab3 雪球人气榜 | v2.7新增 |
| `.spot_cache.pkl` | ashare_movers.py | 内部缓存 | 10分钟TTL |
| `kpi_history.json` | ashare_overview.py | Tab1 Col1 30日排名 | v3.0新增，DuckDB导出最近30条 |

---

## 5. 前端结构

### 5.1 文件

`~/huipan/static/index.html` — 单文件，无构建依赖

### 5.2 三Tab

| Tab | 内容 | 状态 |
|-----|------|------|
| 股票 | 五列布局，数据驱动，30日排名+涨跌分布 | ✅ v3.0 |
| 大宗商品 | Trading Economics 4分类，排序+热力图 | ✅ v2.6 |
| 新闻 | 雪球人气榜 + 英文新闻流 + TradingView经济日历 | ✅ v2.7 |

### 5.3 Tab1 股票 — 五列布局

```
160px | 1fr | 220px | 220px | 220px
 Col1  Col2   Col3    Col4    Col5
```

| 列 | 内容 | 数据源JSON |
|----|------|-----------|
| Col1 | A股KPI（成交额/涨跌比百分比条+全市场分布弹窗/涨跌停/连涨连跌/创新高新低三周期/大中小盘+涨跌分布/30日排名） | `ashare_overview.json` + `kpi_history.json` |
| Col2 | 板块热力图(14个) + ETF热点(涨跌幅默认/成交额) | `ashare_overview.json` |
| Col3 | A股涨跌榜（涨幅/跌幅/成交额 tab切换） | `ashare_movers.json` |
| Col4 | 港股（涨幅/跌幅/成交额/热门 tab切换） | `hk_movers.json` + `global_market.json` |
| Col5 | 美股（涨幅/跌幅/成交量/板块ETF tab切换） | `us_movers.json` + `us_sectors.json` + `global_market.json` |

### 5.4 Tab2 大宗商品

- 懒加载：首次点击Tab2才fetch `commodities.json`
- 4个分类表格（能源/金属/工业/指数），每列可排序（升/降）
- 热力图着色：仅数字颜色，无背景色
- 排序函数 `cmSortBy(sIdx, colIdx)`，状态存 `cmSort[sIdx]`

### 5.5 Tab3 新闻 — 三栏布局

| 栏 | 内容 | 数据源 |
|----|------|--------|
| 左栏 | 雪球人气榜（关注/讨论/交易 × 新增/热门，各Top50） | `hot_rank.json` |
| 中栏 | 英文新闻流（6源，含搜索+来源过滤） | `news.json` |
| 右栏 | TradingView经济日历Widget | 外部iframe |

### 5.6 数据加载流程

```javascript
loadAll()
  → 并行fetch 7个JSON（含kpi_history.json）
  → 数据字段归一化（港股hk_gainers→gainers, hk_hot→hot, 美股most_active→volume, 全球行情by_category→扁平）
  → kpiHistory = kpi_history.json    // 30日排名数据
  → renderOverview(overview)         // Col1 KPI + 排名 + 涨跌分布 + Col2 热力图/ETF
  → renderStockList('cn', data)      // Col3 A股
  → renderStockList('hk', data)      // Col4 港股（含热门tab）
  → renderStockList('us', data)      // Col5 美股
  → renderUSEtfList(sectors)         // Col5 板块ETF tab
  → setStatus()                      // 顶栏状态
```

### 5.7 热力图渲染逻辑

- 取14个板块（涨幅前7 + 跌幅前7），按绝对值降序
- 第1个板块 `grid-column: span 2`（大格），其余1格
- 3行×5列=15格，14个板块+1个span 2=刚好填满
- 颜色：涨红跌绿，透明度 = `min(abs/8 + 0.1, 0.65)`
- 近零（±0.05%以内）显示灰底边框

### 5.8 前端数据字段归一化

后端JSON字段名与前端期望不一致，在loadAll()中统一映射：

| JSON实际字段 | 前端期望 | 处理 |
|---|---|---|
| `hk_gainers/losers/volume` | `gainers/losers/volume` | 别名映射 |
| `hk_hot` | `hot` | 别名映射（v2.8新增） |
| `volume_hkd` | `amount` | 港股成交额字段 |
| `most_active` | `volume` | 美股成交量列表 |
| `by_category.hk_index/us_index` | `hsi/hstech/nasdaq/sp500/dji` | 全球行情扁平化 |
| `items` | `sectors` | 美股ETF列表 |

### 5.9 数据路径配置

```javascript
const DATA = '/static/data/';  // WSL dev with http.server
// const DATA = '/data/';       // ECS production with Nginx
```

---

## 6. 已有模块

- `collector/news.py` ✅ — 6源新闻采集
- `collector/us_market.py` ✅ — Yahoo榜单(含volume_str)+Sina ETF
- `collector/global_market.py` ✅ — Sina全球行情（恒生解析已修复 parts[8]）
- `collector/hk_market.py` ✅ — 新浪全港股+热门港股API（v2.8重写）
- `collector/ashare_movers.py` ✅ — Sina源全市场行情+缓存（v2.6重写）
- `collector/ashare_overview.py` ✅ — 同花顺板块/ETF全市场+三周期新高新低+涨跌分布+KPI快照（v3.0更新）
- `collector/te_commodities.py` ✅ — Trading Economics大宗商品（v2.6）
- `collector/hot_rank.py` ✅ — 雪球人气榜4榜（v2.7）
- `storage/duckdb_v2_store.py` ✅ — 5张表入库
- `data_export.py` ✅ — JSON导出（5文件+archive）
- `config/etf_holdings.json` ✅ — 21只ETF持仓+A股映射
- `config/etf_list.json` ✅ — 48只A股ETF（备用，已被全市场动态查询替代）
- `config/keywords.json` ✅ — 关键词配置
- `preview.html` ✅ — 数据预览页
- `refresh.sh` ✅ — 一键刷新（先movers后overview保证缓存）

---

## 7. 数据库表设计（DuckDB，WSL本地）

沿用v2.3的5张表（`news_articles` / `us_sector_daily` / `us_stock_movers` / `global_market` / `hk_stock_movers`），详见v2.3文档。

**v3.0新增 `kpi_daily_snapshot` 表**（30日排名用）：
- 主键：`date DATE`
- 字段：`volume_total`, `up_count`, `down_count`, `flat_count`, `limit_up`, `limit_down`, `consecutive_up_3`, `consecutive_down_3`, `high_month`, `low_month`, `high_year`, `low_year`, `high_ath`, `low_ath`, `fetched_at`
- 写入时机：`ashare_overview.py` 每次采集完成后 INSERT OR REPLACE
- 导出：最近30条 → `kpi_history.json`

ashare_movers和ashare_overview **不入DuckDB**（行情数据），直接从AKShare采集写JSON。KPI快照单独入库。

---

## 8. LLM 处理层

| 场景 | 模型 | 频率 | 成本 |
|------|------|------|------|
| 新闻翻译+标签+评分 | Deepseek V3 | 每次采集后 | ~¥0.05/天 |
| 盘前报告 | Claude API | 每天1次 | ~¥0.5/天 |
| 美股→A股关联分析 | Deepseek V3 | 每周1-2次 | ~¥0.1/次 |

---

## 9. 已知问题 & 技术债

| # | 问题 | 影响 | 优先级 |
|---|------|------|--------|
| 1 | 北向资金历史数据全NULL | 前端暂不显示 | 中 |
| 2 | Col2资金流向（融资/大宗折价率）无数据源 | 前端已去掉 | 低 |
| 3 | `stock_zh_a_spot()` 高频调用触发限频 | 每天1-2次即可 | 低 |
| ~~4~~ | ~~大中小盘涨跌分布（v10功能）未实现~~ | ✅ v3.0已实现 | — |
| ~~5~~ | ~~30天排位百分位未实现~~ | ✅ v3.0已实现（DuckDB快照+排名） | — |
| 6 | ETF数据与同花顺略有出入 | Sina源价格 vs 东财源价格微差 | 低 |

---

## 10. 开发约定

### 沿用
- 金额：亿元
- 涨跌幅：`3.5` 而非 `0.035`
- 日期偏移查 `trade_calendar`
- NaN 序列化用 `clean_nan()`
- 文件命名避免 `signal.py` / `calendar.py`
- 新闻ID：`hashlib.md5(f"{source}:{url}").hexdigest()[:12]`
- 关键词配置：`config/keywords.json`
- 新闻48小时窗口
- refresh.sh 一键刷新

### v2.5 新增
- ashare_movers/overview **不入DuckDB**，直接AKShare→JSON
- 前端DATA路径区分WSL(`/static/data/`)和ECS(`/data/`)
- AKShare列名用容错查找（`next(c for c in df.columns if '涨跌幅' in c)`）
- 每个AKShare接口独立try/except，单接口失败不阻塞

### v2.6 新增
- **彻底弃用东财`_em`接口（A股）**，全部换Sina+同花顺
- ashare_movers写pkl缓存，ashare_overview 10分钟内复用
- 涨停/跌停从全市场涨跌幅自算（按ST 5%/创业科创 20%/北交 30%/主板 10%阈值）
- 连续下跌接口列名为"连涨天数"（非"连跌"），用容错匹配
- 恒生指数Sina解析：涨跌幅在 `parts[8]`
- ETF全市场动态查询：`fund_etf_spot_ths()`→Sina批量→本地排序
- 创新高/新低：~~遍历分页累加行数~~ → v2.9扩展为三周期（月/年/历史）
- 前端数据字段归一化
- `us_market.py` 补 `volume_str`
- `stock_zh_a_spot()` 每天只跑1-2次
- `te_commodities.py` 完成（4分类排序热力图）
- Tab2大宗商品前端完成

### v2.7 新增
- Tab3新闻前端完成（三栏：雪球人气榜 + 英文新闻流 + TradingView经济日历）
- `hot_rank.py` 雪球4榜采集器
- 涨跌榜双class属性颜色bug修复

### v2.8 新增
- **港股东财接口迁移完成**：`stock_hk_ggt_components_em()` → `stock_hk_spot()`（新浪源）
- 港股过滤规则：价格 < 0.1 HKD 或 成交额 < 100万 HKD 剔除（仙股+低流动性）
- 新增热门港股：Sina `Market_Center.getHKStockData?node=hot_hk` API（~20只）
- 前端"港股通"→"港股"，新增"热门"tab（4个tab：涨幅/跌幅/成交额/热门）
- 港股从590只港股通标的扩展为~2741只全港股（过滤后~1000只）
- 所有东财`_em`接口彻底清零——A股（v2.6）和港股（v2.8）均已迁移

### v2.9 新增
- **创新高/新低扩展为三周期**：月/年/历史，同花顺网页 board 参数（无board=月, board/2/=年, board/1/=历史）
- AKShare `stock_rank_cxg_ths` 列数bug（v1.18.35仍未修复），继续用HTTP直接爬取
- 10jqka ajax URL 返回401（加了认证），非ajax URL `/rank/cxg/{board}/page/N/free/1/` 可用
- JSON字段：`high_52w/low_52w` → `high_month/low_month + high_year/low_year + high_ath/low_ath`
- 前端向后兼容：同时检查新旧字段名
- **热力图随机混排**：涨幅第一→span 2大格，其余13个 Fisher-Yates 随机打乱（红绿交错）
- **股票列表加链接**：A股→Sina个股页，港股→Sina港股页，美股→Yahoo Finance，hover变黄
- **创新高/新低数字加链接**：点击跳转同花顺对应周期页面

### v3.0 新增
- **30日KPI排名**：DuckDB新建 `kpi_daily_snapshot` 表（15字段），每次采集自动INSERT OR REPLACE，导出最近30条到 `kpi_history.json`
- 前端排名显示：`↑ 8/30` 或 `↓ 2/5`（有几天就按几天排，分母=min(已有天数, 30)）
- 排名应用到：成交额、涨停/跌停、上涨/下跌、连涨≥3/连跌≥3、创新高/新低（三周期各自）
- **大中小微盘涨跌分布**：从 `.spot_cache.pkl` 读取涨跌幅，按指数成分股分4档×涨跌幅11桶统计
  - 大盘=沪深300成分股, 中盘=中证500, 小盘=中证1000, 微盘=其余
  - 成分股通过 `ak.index_stock_cons()` 获取，每半年调整一次
  - 桶：>7%, 5~7%, 3~5%, 1~3%, 0~1%, 0%, -1~0%, -3~-1%, -5~-3%, -7~-5%, <-7%
  - 前端可展开/收起，2×2 grid 布局
- **涨跌比百分比条**：上涨N% / 平N / 下跌N% 文字标注
- **涨停/跌停/连涨/连跌加同花顺链接**：ztboard/dtboard/lxsz/lxxd
- `ashare_overview.json` 新增 `cap_distribution` 字段
- `kpi_history.json` 新增JSON文件（前端并行加载7个JSON）
- 前端 `loadAll()` 并行加载 `kpi_history.json`

### v3.1 新增
- **涨停/跌停链接改为新浪**：`vip.stock.finance.sina.com.cn/mkt/#stock_hs_up|down`（原同花顺ztboard/dtboard链接失效）
- 连涨/连跌/创新高新低链接保留同花顺
- **全市场涨跌分布弹窗**：百分比条下方加入口，单独弹窗显示全市场11桶分布（与大中小微盘弹窗分开）
  - `ashare_overview.json` 新增 `market_distribution` 字段
  - `compute_market_distribution(df)` 函数，复用 `.spot_cache.pkl`
- **雪球人气榜改为3类×2子榜**：关注/讨论/交易 × 新增/热门
  - `hot_rank.py` v3.1重写：6个key（follow_new/hot, tweet_new/hot, deal_new/hot）
  - 前端Tab3人气榜：上排3个类别tab + 下排2个子tab（新增｜热门）
  - 去掉"飙升"tab（原follow_surge），改为follow_new
  - 人气榜点击跳转改为雪球个股页（`xueqiu.com/S/SH{code}`）替代东方财富股吧

---

## 11. 项目文件结构

```
huipan/
├── collector/
│   ├── trade_calendar.py     # 交易日历
│   ├── news.py               # ✅ 新闻采集（6源）
│   ├── us_market.py          # ✅ Yahoo榜单+Sina ETF+volume_str
│   ├── global_market.py      # ✅ Sina全球行情（恒生修复）
│   ├── hk_market.py          # ✅ 新浪全港股+热门（v2.8重写）
│   ├── ashare_movers.py      # ✅ Sina源A股涨跌榜+缓存（v2.6重写）
│   ├── ashare_overview.py    # ✅ 同花顺板块/ETF全市场+涨跌分布+KPI快照+全市场分布（v3.1）
│   ├── te_commodities.py     # ✅ Trading Economics大宗商品（v2.6）
│   └── hot_rank.py           # ✅ 雪球人气榜3类×2子榜（v3.1）
├── storage/
│   └── duckdb_v2_store.py    # ✅ 5张表入库
├── config/
│   ├── keywords.json         # ✅ 关键词配置
│   ├── etf_holdings.json     # ✅ 21只美股ETF持仓映射
│   └── etf_list.json         # ✅ 48只A股ETF（备用）
├── static/
│   ├── index.html            # ✅ 三Tab前端（v3.1：全市场分布+人气榜改版+链接优化）
│   └── data/                 # JSON数据文件
│       ├── ashare_overview.json
│       ├── ashare_movers.json
│       ├── .spot_cache.pkl      # 全市场行情缓存
│       ├── kpi_history.json       # v3.0 30日KPI快照（DuckDB导出）
│       ├── hk_movers.json
│       ├── us_movers.json
│       ├── us_sectors.json
│       ├── global_market.json
│       ├── news.json
│       ├── commodities.json
│       └── hot_rank.json
├── data/
│   └── huipan.duckdb
├── data_export.py            # ✅ JSON导出
├── refresh.sh                # ✅ 一键刷新
├── preview.html              # ✅ 数据预览
└── config.py
```

---

## 12. 下一步开发顺序

1. ~~验证数据源可用性~~ ✅
2. ~~建表脚本~~ ✅（5张表）
3. ~~collector/news.py~~ ✅
4. ~~collector/us_market.py~~ ✅
5. ~~collector/global_market.py~~ ✅
6. ~~collector/hk_market.py~~ ✅
7. ~~duckdb_v2_store.py~~ ✅
8. ~~data_export.py~~ ✅
9. ~~config/etf_holdings.json~~ ✅
10. ~~preview.html~~ ✅
11. ~~refresh.sh~~ ✅
12. ~~前端设计讨论~~ ✅
13. ~~collector/ashare_movers.py~~ ✅
14. ~~collector/ashare_overview.py~~ ✅
15. ~~Tab1前端 index.html~~ ✅
16. ~~WSL测试~~ ✅
17. ~~refresh.sh 加入新采集器~~ ✅
18. ~~数据字段归一化~~ ✅
19. ~~collector/te_commodities.py~~ ✅
20. ~~Tab2前端（大宗商品）~~ ✅
21. ~~Tab3前端（新闻）~~ ✅
22. ~~三Tab合并 + Tab切换逻辑~~ ✅
23. ~~港股接口迁移（东财→新浪）~~ ✅ v2.8
24. **→ ECS Nginx配置 + rsync脚本**
25. scheduler/jobs.py（定时刷新）
26. 集成测试 + 部署
27. LLM新闻翻译（Deepseek V3）

---

*文档随项目迭代更新 · v3.1 · 2026-03-15*
