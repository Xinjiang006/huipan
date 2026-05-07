# 慧盘 · A股市场分析平台

> 同花顺告诉你发生了什么，慧盘告诉你这意味着什么。

## 快速开始

### WSL / 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 初始化数据库 + 历史数据
python init_history.py

# 手动运行一次采集（验证）
python scheduler/jobs.py now

# 启动定时调度器
python scheduler/jobs.py
```

### Docker 部署（推荐用于服务器）

```bash
# 构建镜像
docker build -t huipan .

# 初始化历史数据（首次运行）
docker compose run --rm huipan python init_history.py

# 手动采集验证
docker compose run --rm huipan python scheduler/jobs.py now

# 启动服务（后台常驻）
docker compose up -d
```

## 调度时间

| 时间  | 任务         |
|-------|-------------|
| 08:30 | 盘前数据准备  |
| 15:45 | 收盘后采集    |

非交易日自动跳过。

## 项目结构

```
huipan/
├── collector/
│   ├── trade_calendar.py  # 交易日历
│   ├── market.py          # 大盘情绪 + 涨跌停
│   ├── moneyflow.py       # 北向/主力资金
│   ├── sector.py          # 板块资金流向
│   ├── etf.py             # ETF快照（含份额）
│   └── northbound.py      # 北向资金详情
├── models/
│   └── schema.py          # 标准数据模型
├── storage/
│   └── duckdb_store.py    # DuckDB 存储层
├── scheduler/
│   └── jobs.py            # 定时任务
├── data/                  # DuckDB 数据文件（gitignore）
├── logs/                  # 日志（gitignore）
├── init_history.py        # 历史数据初始化
├── config.py              # 配置
├── Dockerfile
└── docker-compose.yml
```

## 数据约定

- 股票代码：6位字符串，不加前缀（`000001` 而非 `SH000001`）
- 金额单位：亿元
- 涨跌幅：%（`3.5` 而非 `0.035`）
- 日期偏移：必须查 `trade_calendar` 表，不能用 `timedelta`
