"""
dvq — Data Validator Quick

命令行数据校验工具。一行命令就能检查 CSV 的数据问题。

用法:
    dvq check data.csv
    dvq check data.csv --key order_id --required email,amount
    dvq check data.csv --json
    dvq check data.csv --fix out.csv

设计：零依赖，PyPI 一条命令安装。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys

# 本地开发时用相对导入；安装后从 dvq_core 导入
try:
    from dvq_core.validator import to_dict, to_markdown, validate
    from dvq_core.selling import LicenseInfo, check_usage, get_upsell_text, verify_license
except ImportError:
    # 开发模式：直接引用 products/core
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(_here, "..", "core"))
    from validator import to_dict, to_markdown, validate
    from selling import (LicenseInfo, check_usage, get_upsell_text,
                         verify_license)


BANNER = r"""
  ___  __   __ ___
 |   \ \ \ / /| _ \
 | |) | \ V / |   /
 |___/   \_/  |_|_\     Data Validator Quick
"""


def read_input(path: str) -> str:
    """读入 CSV（支持文件或 - 表示 stdin）"""
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def write_fixed_csv(text: str, result, out_path: str) -> int:
    """写出清洗后的 CSV（只保留无高危问题的行）"""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return 0

    lossy = set(result.lossy_rows)
    kept = []
    for i, r in enumerate(rows):
        row_num = i + 1  # 1-based
        if row_num == 1 or row_num not in lossy:
            kept.append(r)

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(kept)

    return len(kept) - 1  # 不含表头


def cmd_check(args):
    """校验命令"""
    # 1. 许可
    lic = LicenseInfo(valid=False, plan="free")
    if args.license:
        lic = verify_license(args.license)
        if not lic.valid:
            print(f"warning: license invalid ({lic.reason}), running as free", file=sys.stderr)

    # 2. 读入
    try:
        text = read_input(args.file)
    except FileNotFoundError:
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # 3. 预估计行数用于配额检查
    est_rows = text.count("\n")
    usage = check_usage(lic, "cli", rows=est_rows)

    if not usage.allowed:
        print(f"\n{usage.upsell_message}\n", file=sys.stderr)
        print(f"Upgrade: {usage.checkout}", file=sys.stderr)
        return 3

    # 4. 校验
    required = [s.strip() for s in (args.required or "").split(",") if s.strip()]
    result = validate(text, required_columns=required, key_column=args.key or None)
    d = to_dict(result)

    # 5. 输出
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print_markdown_report(result, d)

    # 6. 导出修复后的 CSV
    if args.fix:
        kept = write_fixed_csv(text, result, args.fix)
        print(f"\nWrote {kept} clean rows to {args.fix} "
              f"({len(result.lossy_rows)} problem rows removed)")

    # 7. 升级提示（只在真的需要时）
    if usage.should_upsell and not lic.valid:
        print("")
        print("-" * 68)
        print(usage.upsell_message)
        print(f"Upgrade: {usage.checkout}")
        print("-" * 68)

    # 8. 退出码
    if args.fail_on and result.counts.get(args.fail_on, 0) > 0:
        return 1
    if args.min_score and result.score < args.min_score:
        return 1
    return 0


def print_markdown_report(result, d):
    """终端友好的报告"""
    score = d["score"]
    # 分数条
    filled = int(score / 5)
    bar = "#" * filled + "." * (20 - filled)

    print("")
    print("=" * 68)
    print(f"  Data Health Score: {score}/100")
    print(f"  [{bar}]")
    print("=" * 68)
    print(f"  {d['summary']}")
    print("")
    print(f"  Rows     {d['total_rows']}")
    print(f"  Columns  {d['total_columns']}")
    print(f"  High     {d['counts']['high']}")
    print(f"  Medium   {d['counts']['mid']}")
    print(f"  Low      {d['counts']['low']}")
    print(f"  At risk  {len(d['lossy_rows'])} rows would be lost")
    print("")

    # 列画像
    if d["columns"]:
        print("  Columns")
        print("  " + "-" * 64)
        for c in d["columns"]:
            t = c["inferred_type"]
            empty = c["empty_count"]
            uniq = c["unique_count"]
            print(f"    {c['name']:<20} {t:<8} empty={empty:<4} unique={uniq}")

    # 问题清单
    if d["issues"]:
        print("")
        print("  Issues (first 20)")
        print("  " + "-" * 64)
        for it in d["issues"][:20]:
            sev = {"high": "!!", "mid": "! ", "low": "  "}[it["severity"]]
            print(f"    {sev} row {it['row']:<4} {it['column']:<16} {it['reason']}")
            print(f"        value: {it['value'][:40]}")
            print(f"        fix:   {it['fix']}")
        if len(d["issues"]) > 20:
            print(f"    … and {len(d['issues']) - 20} more")


def cmd_license(args):
    """许可管理"""
    if args.activate:
        lic = verify_license(args.activate)
        if lic.valid:
            state_dir = os.path.join(os.getcwd(), ".dvq")
            os.makedirs(state_dir, exist_ok=True)
            with open(os.path.join(state_dir, "license"), "w") as f:
                f.write(args.activate)
            print(f"Activated: {lic.plan} plan, expires {lic.expires}")
            return 0
        else:
            print(f"Invalid license: {lic.reason}")
            return 1

    if args.status:
        state_dir = os.path.join(os.getcwd(), ".dvq")
        lp = os.path.join(state_dir, "license")
        if os.path.exists(lp):
            key = open(lp).read().strip()
            lic = verify_license(key)
            print(f"Plan:    {lic.plan}")
            print(f"Email:   {lic.email}")
            print(f"Expires: {lic.expires}")
            print(f"Valid:   {lic.valid}")
        else:
            print("No license installed (free plan)")
        return 0

    print("Usage: dvq license --activate KEY | --status")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dvq",
        description="Check a CSV for the data problems that break pipelines silently.",
    )
    ap.add_argument("--version", action="version", version="dvq 1.0.0")

    sub = ap.add_subparsers(dest="command")

    c = sub.add_parser("check", help="validate a CSV file")
    c.add_argument("file", help="CSV path, or - for stdin")
    c.add_argument("--key", help="column that should be unique")
    c.add_argument("--required", help="comma-separated required columns")
    c.add_argument("--json", action="store_true", help="output JSON")
    c.add_argument("--fix", metavar="OUT", help="write a cleaned CSV")
    c.add_argument("--license", help="license key")
    c.add_argument("--min-score", type=int, default=0,
                   help="exit 1 if score below this")
    c.add_argument("--fail-on", choices=["high", "mid", "low"],
                   help="exit 1 if any issue of this severity")
    c.set_defaults(func=cmd_check)

    l = sub.add_parser("license", help="manage license")
    l.add_argument("--activate", metavar="KEY")
    l.add_argument("--status", action="store_true")
    l.set_defaults(func=cmd_license)

    args = ap.parse_args(argv)

    if not getattr(args, "func", None):
        print(BANNER)
        ap.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
