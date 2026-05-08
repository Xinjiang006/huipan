"""
慧盘 · 日志统一配置
utils/log_helper.py

入口脚本在每个 job 开头调用 setup_logger()。
其他模块照旧 `from loguru import logger`,无需改动。

配置文件: config/log_config.json
动态切换: 改配置文件,下一个 job 触发时自动生效,不用重启容器。

支持的操作:
    python -m utils.log_helper test   # 测试日志输出
    python -m utils.log_helper reload # 强制重载配置
"""

import json
import sys
from pathlib import Path
from loguru import logger

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "log_config.json"
LOG_DIR = BASE_DIR / "logs"

DEFAULT_CONFIG = {
    "console_level": "INFO",
    "file_level": "DEBUG",
    "retention_days": 14,
}

# 缓存当前配置,配置未变化时不重建 sink
_current_config = None


def _read_config() -> dict:
    """读取日志配置,失败时返回默认值"""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            config.update(user_cfg)
        except Exception as e:
            # logger 可能还没初始化,用 print 兜底
            print(f"[log_helper] 配置读取失败: {e},用默认值", file=sys.stderr)
    return config


def setup_logger(force: bool = False):
    """初始化或重配置 logger

    幂等安全: 每个 job 入口调用,配置未变时只读文件比对(几毫秒)
    force=True: 强制重建 sink(测试或手动 reload 用)
    """
    global _current_config
    config = _read_config()

    # 配置未变则跳过(避免每次 job 都重建 sink)
    if not force and _current_config == config:
        return

    _current_config = config
    LOG_DIR.mkdir(exist_ok=True)

    # 清空旧 sink
    logger.remove()

    # 控制台 sink(级别可切)
    logger.add(
        sys.stderr,
        level=config["console_level"],
        format=(
            "<green>{time:HH:mm:ss}</green> "
            "<level>{level: <7}</level> "
            "<cyan>{module}</cyan> | {message}"
        ),
    )

    # 文件 sink(按天轮转,配置保留天数)
    logger.add(
        LOG_DIR / "huipan_{time:YYYYMMDD}.log",
        level=config["file_level"],
        rotation="00:00",
        retention=f"{config['retention_days']} days",
        encoding="utf-8",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} "
            "{level: <7} "
            "{module}:{function}:{line} | {message}"
        ),
    )

    logger.info(
        f"[log] 初始化: 控制台={config['console_level']} "
        f"文件={config['file_level']} 保留={config['retention_days']}天"
    )


# ═══════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="慧盘日志配置工具"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="test",
        choices=["test", "reload"],
        help="test=测试日志输出, reload=强制重载配置",
    )
    args = parser.parse_args()

    if args.action == "reload":
        setup_logger(force=True)
        logger.info("日志配置已强制重载")
    else:
        setup_logger()
        logger.debug("这是 DEBUG 级别消息(默认控制台不显示)")
        logger.info("这是 INFO 级别消息")
        logger.warning("这是 WARNING 级别消息")
        logger.error("这是 ERROR 级别消息")
        logger.info(f"日志文件目录: {LOG_DIR}")
        logger.info(f"配置文件路径: {CONFIG_PATH}")
