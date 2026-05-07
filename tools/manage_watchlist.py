#!/usr/bin/env python3
"""
慧盘 · watchlist 管理工具 v4.0
纯标准库，宿主机直接运行，不需要进Docker

用法:
  python tools/manage_watchlist.py list
  python tools/manage_watchlist.py add-trend 光通信 --thesis "AI算力→光模块需求" --us gb_lite:Lumentum,gb_cohr:Coherent --a 300308:中际旭创,603236:新易盛
  python tools/manage_watchlist.py add-sector 逆变器 --codes 300274:阳光电源,600438:通威股份,688390:固德威
  python tools/manage_watchlist.py add-stock optical --us gb_aaoi:AAOI
  python tools/manage_watchlist.py add-stock inverter --a 002623:亚玛顿
  python tools/manage_watchlist.py remove-stock optical --us gb_cohr
  python tools/manage_watchlist.py remove-stock inverter --a 300274
  python tools/manage_watchlist.py archive optical
  python tools/manage_watchlist.py delete optical
  python tools/manage_watchlist.py set-rule optical --pullback 12 --volume 30
  python tools/manage_watchlist.py set-rule inverter --pullback 10 --volume 30 --sigma 2.0 --excess 5
"""

import json
import sys
import os
import re
from datetime import date
from pathlib import Path

# 自动定位config/watchlist.json
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.json"


def load_watchlist() -> dict:
    """加载watchlist，不存在则返回空结构"""
    if not WATCHLIST_PATH.exists():
        return {"trends": [], "sectors": []}
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(data: dict):
    """原子写入watchlist"""
    tmp = str(WATCHLIST_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(WATCHLIST_PATH))
    print(f"✅ 已保存: {WATCHLIST_PATH}")


def make_id(name: str) -> str:
    """中文名→英文id（简单拼音首字母或直接用小写ascii）"""
    # 如果名字全是ascii，直接小写
    ascii_part = re.sub(r'[^a-zA-Z0-9]', '', name).lower()
    if ascii_part:
        return ascii_part
    # 否则用时间戳兜底
    return f"item_{date.today():%Y%m%d}"


def find_item(data: dict, item_id: str):
    """在trends和sectors中查找，返回 (pool_name, index, item) 或 None"""
    for pool in ["trends", "sectors"]:
        for i, item in enumerate(data[pool]):
            if item["id"] == item_id:
                return pool, i, item
    return None


def parse_symbols(text: str, kind: str) -> list:
    """解析 code:name,code:name 格式"""
    result = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            code, name = part.split(":", 1)
        else:
            code, name = part, part
        if kind == "us":
            # 确保gb_前缀
            source = "sina"
            if not code.startswith("gb_") and not code.startswith("hk"):
                code = "gb_" + code.lower()
            result.append({"code": code, "name": name, "source": source})
        else:
            # A股6位代码
            code = code.strip()
            result.append({"code": code, "name": name})
    return result


# ═══ 命令实现 ═══

def cmd_list(data: dict):
    """列出所有趋势和板块"""
    print("\n═══ 趋势池 ═══")
    if not data["trends"]:
        print("  (空)")
    for t in data["trends"]:
        status = "🟢" if t.get("status") == "active" else "⚫"
        us_names = [u.get("name", u["code"]) for u in t.get("us_symbols", [])]
        a_names = [a.get("name", a["code"]) for a in t.get("a_symbols", [])]
        rule = t.get("entry_rule", {})
        print(f"  {status} [{t['id']}] {t['name']}  (added: {t.get('added', '?')})")
        if t.get("thesis"):
            print(f"     论点: {t['thesis']}")
        print(f"     美股: {', '.join(us_names) if us_names else '(无)'}")
        print(f"     A股:  {', '.join(a_names) if a_names else '(无)'}")
        print(f"     规则: 回调≥{rule.get('pullback_pct', '?')}% 缩量≥{rule.get('volume_shrink_pct', '?')}%")

    print("\n═══ 板块池 ═══")
    if not data["sectors"]:
        print("  (空)")
    for s in data["sectors"]:
        status = "🟢" if s.get("status") == "active" else "⚫"
        codes = s.get("codes", [])
        # codes可能是字符串列表或dict列表
        if codes and isinstance(codes[0], dict):
            names = [c.get("name", c["code"]) for c in codes]
        else:
            names = codes
        rule = s.get("entry_rule", {})
        print(f"  {status} [{s['id']}] {s['name']}  ({len(codes)}只, added: {s.get('added', '?')})")
        print(f"     成分: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
        print(f"     规则: 回调≥{rule.get('pullback_pct', '?')}% 缩量≥{rule.get('volume_shrink_pct', '?')}% σ≥{rule.get('min_sigma', '?')} 超额≥{rule.get('vs_market_excess_pct', '?')}%")
    print()


def cmd_add_trend(data: dict, args: list):
    """添加趋势"""
    if len(args) < 1:
        print("用法: add-trend <名称> [--id ID] [--thesis TEXT] [--us code:name,...] [--a code:name,...] [--pullback N] [--volume N]")
        return
    name = args[0]
    trend_id = None
    thesis = ""
    us_text = ""
    a_text = ""
    pullback = 12
    volume = 30

    i = 1
    while i < len(args):
        if args[i] == "--id" and i + 1 < len(args):
            trend_id = args[i + 1]; i += 2
        elif args[i] == "--thesis" and i + 1 < len(args):
            thesis = args[i + 1]; i += 2
        elif args[i] == "--us" and i + 1 < len(args):
            us_text = args[i + 1]; i += 2
        elif args[i] == "--a" and i + 1 < len(args):
            a_text = args[i + 1]; i += 2
        elif args[i] == "--pullback" and i + 1 < len(args):
            pullback = float(args[i + 1]); i += 2
        elif args[i] == "--volume" and i + 1 < len(args):
            volume = float(args[i + 1]); i += 2
        else:
            print(f"未知参数: {args[i]}"); return

    if not trend_id:
        trend_id = make_id(name)

    # 检查重复
    if find_item(data, trend_id):
        print(f"❌ ID '{trend_id}' 已存在"); return

    trend = {
        "id": trend_id,
        "name": name,
        "thesis": thesis,
        "us_symbols": parse_symbols(us_text, "us") if us_text else [],
        "a_symbols": parse_symbols(a_text, "a") if a_text else [],
        "keywords": [name],
        "entry_rule": {"pullback_pct": pullback, "volume_shrink_pct": volume},
        "added": str(date.today()),
        "status": "active",
    }
    data["trends"].append(trend)
    save_watchlist(data)
    print(f"✅ 趋势 [{trend_id}] {name} 已添加")
    print(f"   美股: {len(trend['us_symbols'])}只  A股: {len(trend['a_symbols'])}只")


def cmd_add_sector(data: dict, args: list):
    """添加板块"""
    if len(args) < 1:
        print("用法: add-sector <名称> [--id ID] --codes code:name,... [--pullback N] [--volume N] [--sigma N] [--excess N]")
        return
    name = args[0]
    sector_id = None
    codes_text = ""
    pullback = 10
    volume = 30
    sigma = 2.0
    excess = 5

    i = 1
    while i < len(args):
        if args[i] == "--id" and i + 1 < len(args):
            sector_id = args[i + 1]; i += 2
        elif args[i] == "--codes" and i + 1 < len(args):
            codes_text = args[i + 1]; i += 2
        elif args[i] == "--pullback" and i + 1 < len(args):
            pullback = float(args[i + 1]); i += 2
        elif args[i] == "--volume" and i + 1 < len(args):
            volume = float(args[i + 1]); i += 2
        elif args[i] == "--sigma" and i + 1 < len(args):
            sigma = float(args[i + 1]); i += 2
        elif args[i] == "--excess" and i + 1 < len(args):
            excess = float(args[i + 1]); i += 2
        else:
            print(f"未知参数: {args[i]}"); return

    if not sector_id:
        sector_id = make_id(name)
    if not codes_text:
        print("❌ 必须指定 --codes"); return

    if find_item(data, sector_id):
        print(f"❌ ID '{sector_id}' 已存在"); return

    parsed = parse_symbols(codes_text, "a")
    codes = [p["code"] for p in parsed]

    sector = {
        "id": sector_id,
        "name": name,
        "codes": codes,
        "entry_rule": {
            "pullback_pct": pullback,
            "volume_shrink_pct": volume,
            "min_sigma": sigma,
            "vs_market_excess_pct": excess,
        },
        "added": str(date.today()),
        "status": "active",
    }
    data["sectors"].append(sector)
    save_watchlist(data)
    print(f"✅ 板块 [{sector_id}] {name} 已添加 ({len(codes)}只)")


def cmd_add_stock(data: dict, args: list):
    """向已有趋势/板块添加股票"""
    if len(args) < 1:
        print("用法: add-stock <id> --us code:name,... 或 --a code:name,..."); return

    item_id = args[0]
    us_text = ""
    a_text = ""

    i = 1
    while i < len(args):
        if args[i] == "--us" and i + 1 < len(args):
            us_text = args[i + 1]; i += 2
        elif args[i] == "--a" and i + 1 < len(args):
            a_text = args[i + 1]; i += 2
        else:
            print(f"未知参数: {args[i]}"); return

    result = find_item(data, item_id)
    if not result:
        print(f"❌ 未找到 '{item_id}'"); return

    pool, idx, item = result
    added = 0

    if pool == "trends":
        if us_text:
            new_us = parse_symbols(us_text, "us")
            existing = {s["code"] for s in item.get("us_symbols", [])}
            for s in new_us:
                if s["code"] not in existing:
                    item.setdefault("us_symbols", []).append(s)
                    added += 1
                    print(f"  + 美股 {s['name']} ({s['code']})")
                else:
                    print(f"  ⏭ {s['code']} 已存在")
        if a_text:
            new_a = parse_symbols(a_text, "a")
            existing = {s["code"] for s in item.get("a_symbols", [])}
            for s in new_a:
                if s["code"] not in existing:
                    item.setdefault("a_symbols", []).append(s)
                    added += 1
                    print(f"  + A股 {s['name']} ({s['code']})")
                else:
                    print(f"  ⏭ {s['code']} 已存在")
    elif pool == "sectors":
        if a_text:
            new_a = parse_symbols(a_text, "a")
            existing = set(item.get("codes", []))
            for s in new_a:
                if s["code"] not in existing:
                    item.setdefault("codes", []).append(s["code"])
                    added += 1
                    print(f"  + {s['name']} ({s['code']})")
                else:
                    print(f"  ⏭ {s['code']} 已存在")
        if us_text:
            print("⚠️ 板块池不支持美股标的")

    if added > 0:
        save_watchlist(data)
        print(f"✅ 已添加{added}只到 [{item_id}]")
    else:
        print("无新增")


def cmd_remove_stock(data: dict, args: list):
    """从趋势/板块移除股票"""
    if len(args) < 1:
        print("用法: remove-stock <id> --us code,... 或 --a code,..."); return

    item_id = args[0]
    us_codes = []
    a_codes = []

    i = 1
    while i < len(args):
        if args[i] == "--us" and i + 1 < len(args):
            us_codes = [c.strip() for c in args[i + 1].split(",")]; i += 2
        elif args[i] == "--a" and i + 1 < len(args):
            a_codes = [c.strip() for c in args[i + 1].split(",")]; i += 2
        else:
            print(f"未知参数: {args[i]}"); return

    result = find_item(data, item_id)
    if not result:
        print(f"❌ 未找到 '{item_id}'"); return

    pool, idx, item = result
    removed = 0

    if pool == "trends":
        if us_codes:
            before = len(item.get("us_symbols", []))
            item["us_symbols"] = [s for s in item.get("us_symbols", []) if s["code"] not in us_codes]
            removed += before - len(item["us_symbols"])
        if a_codes:
            before = len(item.get("a_symbols", []))
            item["a_symbols"] = [s for s in item.get("a_symbols", []) if s["code"] not in a_codes]
            removed += before - len(item["a_symbols"])
    elif pool == "sectors":
        if a_codes:
            before = len(item.get("codes", []))
            item["codes"] = [c for c in item.get("codes", []) if c not in a_codes]
            removed += before - len(item["codes"])

    if removed > 0:
        save_watchlist(data)
        print(f"✅ 已从 [{item_id}] 移除{removed}只")
    else:
        print("无匹配项可移除")


def cmd_archive(data: dict, args: list):
    """归档（保留数据但不再计算）"""
    if len(args) < 1:
        print("用法: archive <id>"); return
    result = find_item(data, args[0])
    if not result:
        print(f"❌ 未找到 '{args[0]}'"); return
    _, _, item = result
    item["status"] = "archived"
    save_watchlist(data)
    print(f"✅ [{args[0]}] {item['name']} 已归档（不再计算，数据保留）")


def cmd_delete(data: dict, args: list):
    """彻底删除"""
    if len(args) < 1:
        print("用法: delete <id>"); return
    result = find_item(data, args[0])
    if not result:
        print(f"❌ 未找到 '{args[0]}'"); return
    pool, idx, item = result

    confirm = input(f"确认删除 [{args[0]}] {item['name']}? (y/N): ")
    if confirm.lower() != 'y':
        print("取消"); return

    data[pool].pop(idx)
    save_watchlist(data)
    print(f"✅ [{args[0]}] 已删除")


def cmd_set_rule(data: dict, args: list):
    """修改入场规则"""
    if len(args) < 1:
        print("用法: set-rule <id> [--pullback N] [--volume N] [--sigma N] [--excess N]"); return

    item_id = args[0]
    result = find_item(data, item_id)
    if not result:
        print(f"❌ 未找到 '{item_id}'"); return
    _, _, item = result
    rule = item.setdefault("entry_rule", {})

    i = 1
    changed = []
    while i < len(args):
        if args[i] == "--pullback" and i + 1 < len(args):
            rule["pullback_pct"] = float(args[i + 1])
            changed.append(f"回调≥{rule['pullback_pct']}%"); i += 2
        elif args[i] == "--volume" and i + 1 < len(args):
            rule["volume_shrink_pct"] = float(args[i + 1])
            changed.append(f"缩量≥{rule['volume_shrink_pct']}%"); i += 2
        elif args[i] == "--sigma" and i + 1 < len(args):
            rule["min_sigma"] = float(args[i + 1])
            changed.append(f"σ≥{rule['min_sigma']}"); i += 2
        elif args[i] == "--excess" and i + 1 < len(args):
            rule["vs_market_excess_pct"] = float(args[i + 1])
            changed.append(f"超额≥{rule['vs_market_excess_pct']}%"); i += 2
        else:
            print(f"未知参数: {args[i]}"); return

    if changed:
        save_watchlist(data)
        print(f"✅ [{item_id}] 规则已更新: {', '.join(changed)}")


def cmd_activate(data: dict, args: list):
    """重新激活已归档的趋势/板块"""
    if len(args) < 1:
        print("用法: activate <id>"); return
    result = find_item(data, args[0])
    if not result:
        print(f"❌ 未找到 '{args[0]}'"); return
    _, _, item = result
    item["status"] = "active"
    save_watchlist(data)
    print(f"✅ [{args[0]}] {item['name']} 已激活")


# ═══ 主入口 ═══

COMMANDS = {
    "list": (cmd_list, "列出所有趋势和板块"),
    "add-trend": (cmd_add_trend, "添加趋势 (add-trend <名称> --us ... --a ...)"),
    "add-sector": (cmd_add_sector, "添加板块 (add-sector <名称> --codes ...)"),
    "add-stock": (cmd_add_stock, "添加股票到已有趋势/板块"),
    "remove-stock": (cmd_remove_stock, "从趋势/板块移除股票"),
    "archive": (cmd_archive, "归档（停止计算但保留数据）"),
    "activate": (cmd_activate, "重新激活已归档项"),
    "delete": (cmd_delete, "彻底删除"),
    "set-rule": (cmd_set_rule, "修改入场规则阈值"),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print("慧盘 · watchlist 管理工具")
        print(f"配置文件: {WATCHLIST_PATH}")
        print()
        for cmd, (_, desc) in COMMANDS.items():
            print(f"  {cmd:16s} {desc}")
        print()
        print("示例:")
        print('  python tools/manage_watchlist.py add-trend AI光通信 --id optical --thesis "AI算力→光模块需求" --us gb_lite:Lumentum --a 300308:中际旭创')
        print('  python tools/manage_watchlist.py add-sector 逆变器 --id inverter --codes 300274:阳光电源,600438:通威股份')
        print('  python tools/manage_watchlist.py add-stock optical --a 002281:光迅科技')
        print('  python tools/manage_watchlist.py remove-stock optical --us gb_cohr')
        print('  python tools/manage_watchlist.py set-rule inverter --pullback 10 --sigma 2.5')
        print('  python tools/manage_watchlist.py archive optical')
        print('  python tools/manage_watchlist.py list')
        return

    cmd_name = sys.argv[1]
    if cmd_name not in COMMANDS:
        print(f"❌ 未知命令: {cmd_name}")
        print(f"可用命令: {', '.join(COMMANDS.keys())}")
        return

    data = load_watchlist()
    func = COMMANDS[cmd_name][0]

    if cmd_name == "list":
        func(data)
    else:
        func(data, sys.argv[2:])


if __name__ == "__main__":
    main()
