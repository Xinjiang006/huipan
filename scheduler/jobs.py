"""
慧盘 · 定时调度器 v5.0
APScheduler + DuckDB交易日历 + 任务分组 + Sina反扒保护 + 功能开关

调度计划（北京时间）：
  交易日：
    05:00±10  美股+全球 + 猎手(美股端)
    09:28     竞价（ashare + opening_picks + intraday第1点 + 暗流盘中 + 板块流量）← 固定
    09:50±10  港股+商品+热榜（disabled模块自动跳过）
    10:30±3   A股 + 盘中追踪 + 暗流盘中 + 板块流量（盘中短jitter）
    13:05±3   A股+商品 + 盘中追踪 + 暗流盘中 + 板块流量（盘中短jitter）
    14:30±3   A股 + 盘中追踪 + 暗流盘中 + 板块流量（恢复ashare刷新pkl）
    15:10±10  A股+商品 + regime + 盘中追踪 + 暗流盘中 + 板块流量 + 猎手(全量) + 反转猎手 + pkl备份
    16:10±10  港股（16:00收盘后）
    16:40     延迟统计（同花顺连涨连跌+新高新低，等数据源刷新）+ 延迟数据传播
  每天：
    新闻每3小时（需 news 启用）

v5.0变更：
  - 16:40 延迟统计后自动传播 delayed 数据到 regime_history / kpi_history（new_high_low_diff, 连涨连跌等）

v4.9变更：
  - 盘中采集点(10:30/13:05/14:30) jitter从±10min缩至±3min，减少数据时间偏差
  - 13:12→13:05，恢复原设计时间
  - 14:30恢复ashare采集，解决pkl过期~70min问题（sina间隔由15:10 guard自动等待）
  - 集成 reversal_tracker（v4.7），15:10收盘链 watchlist后执行
v4.6变更：
  - 各采集点追加 sector_continuity（板块流量：续涨/新进/退出 1d/3d/5d，读pkl+picks_history）
  - 新增 sector_continuity feature开关
v4.2变更：
  - 各采集点追加 derived_intraday（暗流盘中衍生指标，读pkl+腾讯指数，零Sina调用）
  - 新增 derived feature开关
v4.0变更：
  - 05:00 追加 watchlist_tracker(us_only) — 美股端+新闻热度
  - 15:10 追加 watchlist_tracker(full) — regime后、picks前、archive前
  - 新增 watchlist feature开关
"""

import subprocess
import sys
import os
import time
import random
import json
import shutil
from datetime import date, datetime
from pathlib import Path

import duckdb
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

# ── 路径配置 ──
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "huipan.duckdb"
SPOT_CACHE = BASE_DIR / "static" / "data" / ".spot_cache.pkl"
#ARCHIVE_DIR = BASE_DIR / "static" / "data" / "archive"
ARCHIVE_DIR = BASE_DIR / "static" / "data" / "archive" / "spot"
FEATURES_PATH = BASE_DIR / "config" / "features.json"

# ── 功能开关 ──
def _load_features() -> dict:
    """读取 config/features.json，失败时全部启用
    兼容两种格式: {"modules": {...}} 和 直接 {...}
    """
    try:
        with open(FEATURES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 优先取 modules 子key，没有则直接用顶层
        return data.get("modules", data) if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"features.json 读取失败: {e}，全部模块启用")
        return {}

FEATURES = _load_features()

def feature_enabled(name: str) -> bool:
    """检查模块是否启用（默认启用，只有显式False才禁用）"""
    return FEATURES.get(name, True) is not False

# 任务组 → feature名映射（大部分同名，us组对应us_market）
_GROUP_FEATURE = {
    "ashare": "ashare", "hk": "hk", "commodity": "commodity",
    "regime": "regime", "intraday": "intraday",
    "us": "us_market", "news": "news", "hotrank": "hotrank",
    "watchlist": "watchlist",
    "derived": "derived",
    "sector_continuity": "sector_continuity",
    "reversal": "reversal",
}

# ── 日志配置 ──
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(
    LOG_DIR / "scheduler_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
    encoding="utf-8",
)

# ── Sina全市场调用时间戳（反扒保护）──
_last_sina_spot_time = 0.0
SINA_MIN_INTERVAL = 45 * 60  # 45分钟


# ──────────────────────────────────────────
# 
# 在每个盘中采集点的链末尾加入 transition_detector 调用。
# detector依赖所有JSON最新数据，必须在所有采集器之后跑。
#
# 时机：09:28 / 10:30 / 13:05 / 14:30 / 15:10
# 每个时间点的采集链末尾加以下代码块：

# === 粘贴到每个采集点末尾 ===

def _run_transition_detector():
    """转折检测（v4.3），所有采集器之后跑"""
    try:
        from collector.regime_transition_detector import run_transition_detector
        run_transition_detector()
    except Exception as e:
        print(f"  ⚠️ 转折检测跳过: {e}")


def _run_sector_continuity():
    """板块流量分析（v4.6）：今日 Top100 vs 前 1/3/5 日对比
    依赖：spot pkl（最新）+ picks_history.json + sector_map.json
    必须在 ashare / derived 之后跑，确保 pkl 是当前时间点的最新快照。
    """
    if not feature_enabled("sector_continuity"):
        return
    try:
        from collector.sector_continuity import run_sector_continuity
        run_sector_continuity()
    except Exception as e:
        logger.warning(f"  ⚠️ 板块流量分析跳过: {e}")


def _run_reversal_tracker():
    """反转猎手（v4.7）：扫描由弱转强候选股
    依赖：spot pkl + regime_history + picks_history，必须在 regime 之后跑。
    仅 15:10 收盘链调用（盘中跑无意义，需收盘数据确认反转）。
    """
    if not feature_enabled("reversal"):
        return
    try:
        from collector.reversal_tracker import run_reversal_tracker
        run_reversal_tracker()
    except Exception as e:
        logger.warning(f"  ⚠️ 反转猎手跳过: {e}")
    try:
        from collector.reversal_tracker_v57 import run_reversal_tracker as run_reversal_tracker_v57
        run_reversal_tracker_v57()
    except Exception as e:
        logger.warning(f"    反转猎手(v57)跳过: {e}")


def _run_reversal_monitor(slot=None):
    """反转盘中监控（v5.6）：读昨日watchlist + 今日spot，输出reversal_monitor.json
    依赖：reversal_watchlist.json + spot pkl，必须在 derived 之后跑。
    仅 10:30 / 14:30 调用。
    """
    if not feature_enabled("reversal"):
        return
    try:
        from collector.reversal_tracker import monitor_candidates
        monitor_candidates(slot=slot)
    except Exception as e:
        logger.warning(f"  ⚠️ 反转监控跳过: {e}")

# ═══════════════════════════════════════════
# 任务组定义
# ═══════════════════════════════════════════

TASK_GROUPS = {
    "ashare": [
        "collector.ashare_movers",
        "collector.ashare_overview",
    ],
    "hk": [
        "collector.hk_market",
    ],
    "us": [
        "collector.us_market",
        "collector.global_market",
    ],
    "commodity": [
        "collector.te_commodities",
    ],
    "news": [
        "collector.news",
        "collector.investing_news",
    ],
    "hotrank": [
        "collector.hot_rank",
    ],
    "regime": [
        "collector.regime_collector",
    ],
    "intraday": [
        "collector.intraday_tracker",
    ],
    "watchlist": [
        "collector.watchlist_tracker",
    ],
    "derived": [
        "collector.derived_intraday",
    ],
}

# 09:50 开盘其他（不含ashare，不含us）
OPEN_ORDER = ["hk", "commodity", "news", "hotrank"]
# 手动 --run all 用
ALL_ORDER = ["ashare", "hk", "us", "commodity", "news", "hotrank", "regime", "intraday"]


# ═══════════════════════════════════════════
# 交易日判定
# ═══════════════════════════════════════════

def is_trading_day(check_date: date = None) -> bool:
    """查询DuckDB trade_calendar判断是否交易日，失败时回退周一-周五"""
    if check_date is None:
        check_date = date.today()
    try:
        conn = duckdb.connect(str(DB_PATH), read_only=True)
        result = conn.execute(
            "SELECT is_trading FROM trade_calendar WHERE date = ?",
            [check_date],
        ).fetchone()
        conn.close()
        if result is not None:
            return bool(result[0])
    except Exception as e:
        logger.warning(f"trade_calendar查询失败: {e}，回退周一-周五: {check_date.weekday() < 5}")
    return check_date.weekday() < 5


# ═══════════════════════════════════════════
# Sina反扒保护
# ═══════════════════════════════════════════

def sina_guard():
    """检查距上次Sina全市场调用是否≥45分钟，不够则sleep等待"""
    global _last_sina_spot_time
    if _last_sina_spot_time == 0:
        return
    elapsed = time.time() - _last_sina_spot_time
    if elapsed < SINA_MIN_INTERVAL:
        wait = SINA_MIN_INTERVAL - elapsed + 10  # 多等10秒余量
        logger.info(f"⏳ Sina保护: 距上次{elapsed:.0f}s，等待{wait:.0f}s")
        time.sleep(wait)


def sina_mark():
    """标记Sina全市场调用时间"""
    global _last_sina_spot_time
    _last_sina_spot_time = time.time()


# ═══════════════════════════════════════════
# Jitter（随机偏移）
# ═══════════════════════════════════════════

def jitter_sleep():
    """随机等待0~600秒（0~10分钟），用于非盘中敏感时段"""
    wait = random.randint(0, 600)
    if wait > 10:
        logger.info(f"🎲 jitter: +{wait}s ({wait // 60}m{wait % 60}s)")
        time.sleep(wait)


def jitter_sleep_short():
    """随机等待0~180秒（0~3分钟），用于盘中采集点（10:30/13:05/14:30）
    盘中数据时效性敏感，±10min偏差过大，缩至±3min"""
    wait = random.randint(0, 180)
    if wait > 10:
        logger.info(f"🎲 jitter(短): +{wait}s ({wait // 60}m{wait % 60}s)")
        time.sleep(wait)


# ═══════════════════════════════════════════
# 运行采集器（带重试）
# ═══════════════════════════════════════════

def run_collector(module: str, retry=True, extra_env=None) -> bool:
    """运行单个采集器模块，失败时等30秒重试一次
    extra_env: 额外环境变量，如 {"HUIPAN_SKIP_PICKS": "1"}
    """
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}

    logger.info(f"▶ 开始: {module}")
    t0 = time.time()

    for attempt in range(1, 3 if retry else 2):
        try:
            result = subprocess.run(
                [sys.executable, "-m", module],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(BASE_DIR),
                env=env,
            )
            elapsed = time.time() - t0

            if result.returncode == 0:
                logger.info(f"✅ 完成: {module} ({elapsed:.1f}s)")
                return True
            else:
                logger.error(f"❌ 失败: {module} ({elapsed:.1f}s) code={result.returncode}")
                if result.stderr:
                    logger.error(f"   stderr: {result.stderr.strip()[:300]}")
        except subprocess.TimeoutExpired:
            logger.error(f"⏰ 超时: {module} (>300s)")
        except Exception as e:
            logger.error(f"❌ 异常: {module} → {e}")

        # 重试
        if attempt == 1 and retry:
            logger.info(f"🔁 30秒后重试: {module}")
            time.sleep(30)

    return False


def run_group(group_name: str, extra_env=None) -> dict:
    """运行一个任务组（disabled的模块直接跳过）"""
    feat = _GROUP_FEATURE.get(group_name, group_name)
    if not feature_enabled(feat):
        logger.info(f"⏭️ [{group_name}] 已禁用，跳过")
        return {"group": group_name, "total": 0, "success": 0, "failed": 0}

    modules = TASK_GROUPS.get(group_name, [])
    if not modules:
        logger.warning(f"未知任务组: {group_name}")
        return {"group": group_name, "total": 0, "success": 0, "failed": 0}

    logger.info(f"━━━ [{group_name}] 开始 ({len(modules)}个) ━━━")
    success = failed = 0
    for mod in modules:
        if run_collector(mod, extra_env=extra_env):
            success += 1
        else:
            failed += 1
    logger.info(f"━━━ [{group_name}] 完成: ✅{success} ❌{failed} ━━━")
    return {"group": group_name, "total": len(modules), "success": success, "failed": failed}


def run_groups(group_names: list, extra_env=None) -> list:
    """按顺序运行多个任务组"""
    return [run_group(name, extra_env=extra_env) for name in group_names]


def _log_results(label: str, results: list, t0: float):
    """统一输出结果"""
    elapsed = time.time() - t0
    total_s = sum(r["success"] for r in results)
    total_f = sum(r["failed"] for r in results)
    logger.info(f"🏁 ════ {label}: ✅{total_s} ❌{total_f} · {elapsed:.0f}s ════")


# ═══════════════════════════════════════════
# Archive 备份
# ═══════════════════════════════════════════

def archive_spot_cache():
    """备份当天 .spot_cache.pkl"""
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = ARCHIVE_DIR / f"spot_{date.today():%Y%m%d}.pkl"
        if SPOT_CACHE.exists():
            shutil.copy2(str(SPOT_CACHE), str(dest))
            logger.info(f"📦 pkl备份: {dest.name}")
        else:
            logger.warning("📦 pkl不存在，跳过备份")
    except Exception as e:
        logger.error(f"📦 pkl备份失败: {e}")


# ═══════════════════════════════════════════
# 调度任务
# ═══════════════════════════════════════════

def job_us():
    """05:00±10 美股+全球 + 猎手(美股端)"""
    if not is_trading_day():
        logger.info("📅 非交易日，跳过美股")
        return
    jitter_sleep()
    logger.info("🇺🇸 ════ 美股+全球 (05:00) ════")
    t0 = time.time()
    results = run_groups(["us"])

    # v4.0: 猎手 — 美股端+新闻热度（A股pkl此时无数据，us_only跳过）
    if feature_enabled("watchlist"):
        r = run_collector("collector.watchlist_tracker",
                          extra_env={"HUIPAN_US_ONLY": "1"})
        results.append({"group": "watchlist", "total": 1,
                        "success": 1 if r else 0, "failed": 0 if r else 1})

    _log_results("美股+全球+猎手", results, t0)


def job_opening():
    """09:28 竞价采集 + intraday第1点（固定，不加jitter）"""
    if not is_trading_day():
        logger.info("📅 非交易日，跳过竞价")
        return
    logger.info("🔔 ════ 竞价采集 (09:28) ════")
    t0 = time.time()

    sina_guard()
    results = run_groups(["ashare"])
    sina_mark()

    # intraday第1点（读pkl+3个指数，不触发Sina全市场）
    results += run_groups(["intraday"])

    _run_transition_detector()

    # 暗流盘中指标（读pkl+腾讯指数，不触发Sina）
    results += run_groups(["derived"])

    # 板块流量分析（v4.6）
    _run_sector_continuity()

    _log_results("竞价+追踪", results, t0)


def job_open_others():
    """09:50±10 港股+商品+新闻+热榜（不含ashare）"""
    if not is_trading_day():
        logger.info("📅 非交易日，跳过开盘其他")
        return
    jitter_sleep()
    logger.info("🚀 ════ 港股+商品+新闻+热榜 (09:50) ════")
    t0 = time.time()
    results = run_groups(OPEN_ORDER)
    _log_results("开盘其他", results, t0)


def job_mid_1030():
    """10:30±3 A股 + 盘中追踪"""
    if not is_trading_day():
        return
    jitter_sleep_short()
    logger.info("🔄 ════ A股+追踪 (10:30) ════")
    t0 = time.time()

    sina_guard()
    results = run_groups(["ashare", "intraday"])
    _run_transition_detector()
    sina_mark()

    # 暗流盘中指标
    results += run_groups(["derived"])

    # 板块流量分析（v4.6）
    _run_sector_continuity()

    # 反转盘中监控（v5.6）
    _run_reversal_monitor(slot="10:30")

    _log_results("盘中A股+追踪", results, t0)


def job_afternoon():
    """13:05±3 A股+商品 + 盘中追踪（不含港股）"""
    if not is_trading_day():
        return
    jitter_sleep_short()
    logger.info("🔄 ════ A股+商品+追踪 (13:05) ════")
    t0 = time.time()

    sina_guard()
    results = run_groups(["ashare", "commodity", "intraday"])

    _run_transition_detector()

    sina_mark()

    # 暗流盘中指标
    results += run_groups(["derived"])

    # 板块流量分析（v4.6）
    _run_sector_continuity()

    _log_results("下午+追踪", results, t0)


def job_mid_1430():
    """14:30±3 A股 + 盘中追踪（恢复ashare刷新pkl，sina间隔由15:10 guard等待）"""
    if not is_trading_day():
        return
    jitter_sleep_short()
    logger.info("🔄 ════ A股+盘中追踪 (14:30) ════")
    t0 = time.time()

    sina_guard()
    results = run_groups(["ashare", "intraday"])
    sina_mark()

    _run_transition_detector()
    # 暗流盘中指标
    results += run_groups(["derived"])

    # 板块流量分析（v4.6）
    _run_sector_continuity()

    # 反转盘中监控（v5.6）
    _run_reversal_monitor(slot="14:30")

    _log_results("盘中A股+追踪", results, t0)


def _save_deferred_picks():
    """v3.9.2: intraday完成后保存yesterday_picks（从pkl缓存加载）"""
    logger.info("📝 延后保存 yesterday_picks...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "collector.ashare_movers", "--save-picks"],
            capture_output=True, text=True, timeout=60,
            cwd=str(BASE_DIR),
        )
        if result.returncode == 0:
            logger.info("✅ yesterday_picks已保存（延后）")
        else:
            logger.error(f"❌ picks保存失败: {result.stderr.strip()[:300]}")
    except Exception as e:
        logger.error(f"❌ picks保存异常: {e}")


def job_ashare_close(scheduler=None):
    """15:10±10 A股收盘+商品+追踪+猎手+archive
    v4.0执行顺序：
      1. ashare+commodity（SKIP_PICKS延后，内含regime计算）
      2. intraday最终点（此时picks仍是昨天的，T+1正确）
      3. watchlist_tracker全量（regime_history.json已更新）
      4. 保存today picks（供明日regime使用）
      5. pkl备份
    """
    if not is_trading_day():
        return
    jitter_sleep()
    logger.info("🔄 ════ A股收盘 (15:10) ════")
    t0 = time.time()

    # 1. A股+商品（延后picks保存；ashare_movers内部已调collect_regime，不单独跑）
    #    SKIP_THS: 同花顺统计数据延迟到16:40采集
    sina_guard()
    results = run_groups(["ashare", "commodity"],
                         extra_env={"HUIPAN_SKIP_PICKS": "1", "HUIPAN_SKIP_THS": "1"})
    sina_mark()

    # 2. 盘中最终点（picks仍是昨天的 ✅）
    results += run_groups(["intraday"])

    # 2.5 暗流盘中指标最终点
    results += run_groups(["derived"])

    # 2.6 板块流量分析最终点（v4.6，picks_history已含今日，1d/3d/5d完整输出）
    _run_sector_continuity()

    # 3. v4.0: 猎手全量（regime已在ashare内跑完，regime_history.json已更新）
    if feature_enabled("watchlist"):
        results += run_groups(["watchlist"])

    _run_transition_detector()

    # 3.5 v4.7: 反转猎手（依赖 regime + spot pkl，收盘后跑）
    _run_reversal_tracker()

    # 4. intraday完成后，保存today picks供明日使用
    _save_deferred_picks()

    # 5. pkl备份
    archive_spot_cache()  # 收盘后用最新pkl覆盖盘中归档

    _log_results("收盘+regime+猎手+追踪", results, t0)

    # ashare失败时1小时后重试
    ashare_result = next((r for r in results if r["group"] == "ashare"), None)
    if ashare_result and ashare_result["failed"] > 0 and scheduler:
        from datetime import timedelta
        retry_time = datetime.now() + timedelta(hours=1)
        logger.warning(f"⚠️ A股收盘有失败，{retry_time:%H:%M} 重试")
        scheduler.add_job(
            _job_ashare_close_retry,
            "date",
            run_date=retry_time,
            id="ashare_close_retry",
            name="A股收盘重试",
            replace_existing=True,
        )


def _job_ashare_close_retry():
    """~16:10 A股收盘重试（一次性）"""
    if not is_trading_day():
        return
    logger.info("🔄 ════ A股收盘【重试】════")
    t0 = time.time()
    sina_guard()
    results = run_groups(["ashare", "commodity"])
    sina_mark()
    _log_results("A股收盘重试", results, t0)


def job_hk():
    """16:10±10 港股收盘"""
    if not is_trading_day():
        return
    jitter_sleep()
    logger.info("🇭🇰 ════ 港股 (16:10) ════")
    t0 = time.time()
    results = run_groups(["hk"])
    _log_results("港股", results, t0)


def job_delayed_stats():
    """16:40 延迟统计（同花顺连涨连跌+新高新低，收盘后需等待数据源刷新）"""
    if not is_trading_day():
        return
    logger.info("📊 ════ 延迟统计 (16:40) ════")
    t0 = time.time()
    ok = run_collector("collector.ashare_overview",
                       extra_env={"HUIPAN_DELAYED": "1"})
    results = [{"group": "delayed_stats", "total": 1,
                "success": 1 if ok else 0, "failed": 0 if ok else 1}]

    # v5.8: 回写 delayed 数据到 regime_history / kpi_history
    if ok:
        _propagate_delayed_data()

    _log_results("延迟统计", results, t0)


def _propagate_delayed_data():
    """v5.8: 16:40 延迟数据传播 — 把 ashare_overview 的 delayed 字段回写到 regime_history / kpi_history

    16:40 ashare_overview 更新了 new_high_low / 连涨连跌等 THS 延迟数据，
    但 regime_history[0] 和 kpi_history 还是 15:10 的旧值。
    此函数把 delayed 数据回写到下游 JSON，不重跑 regime_collector。
    """
    data_dir = BASE_DIR / "static" / "data"
    try:
        # 1. 读 ashare_overview (已有 delayed 数据)
        overview_path = data_dir / "ashare_overview.json"
        if not overview_path.exists():
            logger.warning("  ⚠️ ashare_overview.json 不存在，跳过传播")
            return
        with open(overview_path, "r", encoding="utf-8") as f:
            overview = json.load(f)
        if not overview.get("delayed_at"):
            logger.info("  ℹ️ ashare_overview 无 delayed_at，跳过传播")
            return

        kpi = overview.get("kpi", {})

        # 2. 读 new_high_low.json
        nhl_path = data_dir / "new_high_low.json"
        nhl_diff = None
        if nhl_path.exists():
            with open(nhl_path, "r", encoding="utf-8") as f:
                nhl = json.load(f)
            today_data = nhl.get("today", {})
            h_year = (today_data.get("high_year") or {}).get("total", 0) or 0
            l_year = (today_data.get("low_year") or {}).get("total", 0) or 0
            nhl_diff = h_year - l_year

        # 3. 回写 regime_history.json[0]
        rh_path = data_dir / "regime_history.json"
        if rh_path.exists():
            with open(rh_path, "r", encoding="utf-8") as f:
                rh = json.load(f)
            if rh and isinstance(rh, list) and rh[0].get("date") == date.today().isoformat():
                patched = []
                # new_high_low_diff
                if nhl_diff is not None and rh[0].get("new_high_low_diff") is None:
                    rh[0]["new_high_low_diff"] = nhl_diff
                    patched.append(f"new_high_low_diff={nhl_diff}")
                # delayed_at 标记
                rh[0]["delayed_at"] = overview.get("delayed_at")

                if patched:
                    tmp = rh_path.with_suffix(".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(rh, f, ensure_ascii=False, indent=2)
                    os.replace(str(tmp), str(rh_path))
                    logger.info(f"  ✅ regime_history 回写: {', '.join(patched)}")
                else:
                    logger.info("  ℹ️ regime_history 无需回写")
            else:
                logger.info("  ℹ️ regime_history[0] 不是今天，跳过回写")

        # 4. 回写 kpi_history.json
        kpi_path = data_dir / "kpi_history.json"
        if kpi_path.exists():
            with open(kpi_path, "r", encoding="utf-8") as f:
                kpi_hist = json.load(f)
            if isinstance(kpi_hist, list) and kpi_hist:
                today_kpi = kpi_hist[0] if kpi_hist[0].get("date") == date.today().isoformat() else None
            elif isinstance(kpi_hist, dict):
                today_kpi = kpi_hist.get(date.today().isoformat())
            else:
                today_kpi = None

            if today_kpi is not None:
                # 同步 delayed 字段
                delayed_fields = {
                    "high_month": kpi.get("high_month"),
                    "low_month": kpi.get("low_month"),
                    "high_year": kpi.get("high_year"),
                    "low_year": kpi.get("low_year"),
                    "consecutive_up_3": kpi.get("consecutive_up_3"),
                    "consecutive_down_3": kpi.get("consecutive_down_3"),
                }
                kpi_patched = []
                for k, v in delayed_fields.items():
                    if v is not None and today_kpi.get(k) != v:
                        today_kpi[k] = v
                        kpi_patched.append(f"{k}={v}")
                if kpi_patched:
                    tmp = kpi_path.with_suffix(".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(kpi_hist, f, ensure_ascii=False, indent=2)
                    os.replace(str(tmp), str(kpi_path))
                    logger.info(f"  ✅ kpi_history 回写: {', '.join(kpi_patched[:5])}...")
                else:
                    logger.info("  ℹ️ kpi_history 无需回写")

        logger.info("✅ 延迟数据传播完成")
    except Exception as e:
        logger.warning(f"  ⚠️ 延迟数据传播失败（不阻塞）: {e}")


def job_news():
    """新闻采集 — 每3小时"""
    logger.info("📰 ════ 新闻采集 ════")
    t0 = time.time()
    result = run_group("news")
    _log_results("新闻", [result], t0)


def job_ashare_close_regime_only():
    """16:45 单独跑regime_collector，补15:10时THS未就绪的字段"""
    try:
        from collector.regime_collector import collect_regime
        collect_regime()
        print("[job] regime补刷完成")
    except Exception as e:
        print(f"[job] regime补刷失败: {e}")

# ═══════════════════════════════════════════
# 启动调度器
# ═══════════════════════════════════════════

def create_scheduler() -> BlockingScheduler:
    """创建并配置调度器（根据features.json跳过禁用模块）"""
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    # 05:00 美股+全球（需 us_market 启用）
    if feature_enabled("us_market"):
        scheduler.add_job(job_us, CronTrigger(hour=5, minute=0),
                          id="us_morning", name="美股+全球 05:00")

    # 09:28 竞价+intraday（固定）
    scheduler.add_job(job_opening, CronTrigger(hour=9, minute=28),
                      id="opening", name="竞价+追踪 09:28")

    # 09:50 港股+商品+新闻+热榜（run_group内部会自动跳过disabled的）
    scheduler.add_job(job_open_others, CronTrigger(hour=9, minute=50),
                      id="open_others", name="港股+商品+热榜 09:50")

    # 10:30 A股+追踪
    scheduler.add_job(job_mid_1030, CronTrigger(hour=10, minute=30),
                      id="mid_1030", name="A股+追踪 10:30")

    # 13:05 A股+商品+追踪
    scheduler.add_job(job_afternoon, CronTrigger(hour=13, minute=5),
                      id="afternoon", name="A股+商品+追踪 13:05")

    # 14:30 追踪only
    scheduler.add_job(job_mid_1430, CronTrigger(hour=14, minute=30),
                      id="mid_1430", name="盘中追踪 14:30")

    # 15:10 收盘+追踪+archive（regime在ashare内部执行）
    scheduler.add_job(lambda: job_ashare_close(scheduler), CronTrigger(hour=15, minute=10),
                      id="ashare_close", name="收盘+追踪 15:10")

    # 16:10 港股
    if feature_enabled("hk"):
        scheduler.add_job(job_hk, CronTrigger(hour=16, minute=10),
                          id="hk_close", name="港股 16:10")

    # 16:40 延迟统计（同花顺数据刷新后采集）
    scheduler.add_job(job_delayed_stats, CronTrigger(hour=16, minute=40),
                      id="delayed_stats", name="延迟统计 16:40")

     # 16:45 regime补刷（THS数据就绪后补new_high_low_diff等字段）
    #     scheduler.add_job(job_ashare_close_regime_only, CronTrigger(hour=16, minute=45),
    #                       id="regime_patch", name="regime补刷 16:45")

    # 新闻每3h（需 news 启用）
    if feature_enabled("news"):
        scheduler.add_job(job_news, CronTrigger(hour="0,3,6,9,12,15,18,21", minute=0),
                          id="news", name="新闻 每3h")

    return scheduler


def start():
    """启动调度器"""
    logger.info("=" * 50)
    logger.info("慧盘调度器启动 v5.0")
    logger.info(f"数据库: {DB_PATH}")
    logger.info(f"今日交易日: {is_trading_day()}")
    # 功能开关
    disabled = [k for k, v in FEATURES.items() if not v]
    enabled = [k for k, v in FEATURES.items() if v]
    logger.info(f"启用模块: {', '.join(enabled) if enabled else '全部'}")
    if disabled:
        logger.info(f"禁用模块: {', '.join(disabled)}")
    logger.info("=" * 50)
    logger.info("调度计划（北京时间）:")
    if feature_enabled("us_market"):
        logger.info("  05:00±10  美股+全球+猎手(美股端)")
    logger.info("  09:28     竞价+intraday+暗流盘中（固定）")
    logger.info("  09:50±10  港股+商品+热榜")
    logger.info("  10:30±3   A股+追踪+暗流盘中+板块流量")
    logger.info("  13:05±3   A股+商品+追踪+暗流盘中+板块流量")
    logger.info("  14:30±3   A股+追踪+暗流盘中+板块流量")
    logger.info("  15:10±10  收盘+追踪+暗流盘中+板块流量+猎手+反转猎手+picks+archive")
    if feature_enabled("hk"):
        logger.info("  16:10±10  港股")
    logger.info("  16:40     延迟统计（连涨连跌+新高新低）+ 传播到regime/kpi")
    if feature_enabled("news"):
        logger.info("  新闻:     每3h")
    logger.info("-" * 50)
    logger.info("保护: Sina间隔≥45min + jitter盘中±3min/其他±10min + 失败重试30s")
    logger.info("=" * 50)

    scheduler = create_scheduler()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器停止")
