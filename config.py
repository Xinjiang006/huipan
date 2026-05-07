"""
慧盘项目配置文件
"""
import os
from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).parent

# 数据库路径
DUCKDB_PATH = BASE_DIR / "data" / "huipan.duckdb"

# 日志路径
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Redis 配置（第二阶段启用）
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

# PostgreSQL 配置（用户体系，第二阶段启用）
PG_DSN = os.getenv("PG_DSN", "postgresql://postgres:password@localhost:5432/huipan")

# DuckDB 内存限制（服务器内存小，注意控制）
DUCKDB_MEMORY_LIMIT = "512MB"

# 调度时间
SCHEDULE_MORNING = "08:30"   # 盘前数据准备
SCHEDULE_EVENING = "15:45"   # 收盘后采集

# AKShare 请求超时
REQUEST_TIMEOUT = 30

# -------------------------------------------------------
# 用 curl_cffi 替换 requests，模拟浏览器 TLS 指纹
# 东财服务器会检测 JA3 指纹，标准 requests 会被直接断开
# -------------------------------------------------------
try:
    from curl_cffi import requests as cffi_requests
    import requests as _requests

    class _CffiSession(_requests.Session):
        """用 curl_cffi 的 chrome 指纹替代标准 TLS"""
        def request(self, method, url, **kwargs):
            kwargs.setdefault("timeout", REQUEST_TIMEOUT)
            kwargs.setdefault("impersonate", "chrome110")
            resp = cffi_requests.request(method, url, **kwargs)
            # 包装成 requests.Response 兼容对象
            r = _requests.Response()
            r.status_code = resp.status_code
            r.headers = dict(resp.headers)
            r._content = resp.content
            r.encoding = resp.encoding or "utf-8"
            r.url = str(resp.url)
            return r

    _requests.Session = _CffiSession
    print("[config] curl_cffi patch 已启用")
except ImportError:
    print("[config] curl_cffi 未安装，使用标准 requests")
