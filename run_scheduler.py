#!/usr/bin/env python3
"""
慧盘 · 调度器入口

用法:
  python run_scheduler.py              # 启动定时守护
  python run_scheduler.py --run all    # 手动跑全量
  python run_scheduler.py --run news   # 只跑新闻
  python run_scheduler.py --run ashare # 只跑A股
  python run_scheduler.py --run hk     # 只跑港股
  python run_scheduler.py --run us     # 只跑美股+全球
  python run_scheduler.py --run commodity  # 只跑大宗商品
  python run_scheduler.py --run hotrank    # 只跑雪球人气榜
  python run_scheduler.py --list       # 列出所有任务组
"""

import argparse
import sys
import time

from loguru import logger
from scheduler.jobs import (
    ALL_ORDER,
    TASK_GROUPS,
    run_group,
    run_groups,
    start,
)


def main():
    parser = argparse.ArgumentParser(description="慧盘调度器")
    parser.add_argument(
        "--run",
        type=str,
        metavar="GROUP",
        help="手动运行指定任务组 (all/ashare/hk/us/commodity/news/hotrank)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出所有任务组",
    )
    args = parser.parse_args()

    # 列出任务组
    if args.list:
        print("\n慧盘采集任务组:")
        print("-" * 45)
        for name, modules in TASK_GROUPS.items():
            print(f"  {name:<12} → {', '.join(modules)}")
        print(f"  {'all':<12} → {' → '.join(ALL_ORDER)}")
        print()
        return

    # 手动运行
    if args.run:
        group = args.run.lower()
        logger.info(f"手动执行: {group}")
        overall_start = time.time()

        if group == "all":
            results = run_groups(ALL_ORDER)
        elif group in TASK_GROUPS:
            results = [run_group(group)]
        else:
            print(f"❌ 未知任务组: {group}")
            print(f"可用: all, {', '.join(TASK_GROUPS.keys())}")
            sys.exit(1)

        # 汇总
        elapsed = time.time() - overall_start
        total_s = sum(r["success"] for r in results)
        total_f = sum(r["failed"] for r in results)
        print(f"\n{'='*40}")
        print(f"执行完成: ✅ {total_s}  ❌ {total_f}  ⏱ {elapsed:.0f}s")
        print(f"{'='*40}")
        sys.exit(1 if total_f > 0 else 0)

    # 默认：启动定时守护
    start()


if __name__ == "__main__":
    main()
