"""
共享校验内核 · Python 实现

与 validator.ts 行为完全一致（同一套规则，同一套输出结构）。
供 Python CLI 和 MCP Server 使用。

零第三方依赖，只用标准库。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from typing import Any

PLACEHOLDERS = {
    "n/a", "na", "n.a.", "null", "nil", "none", "-", "--", "—",
    "unknown", "tbd", "tba", "#n/a", "#value!", "nan", "undefined",
}


@dataclass
class Issue:
    type: str
    severity: str  # high | mid | low
    column: str
    row: int  # 1-based，含表头则数据从 2 开始
    value: str
    reason: str
    fix: str


@dataclass
class ColumnProfile:
    name: str
    index: int
    inferred_type: str  # string | number | date | money | mixed | empty
    total_rows: int
    empty_count: int
    unique_count: int
    type_mismatch_rate: float
    samples: list[str]


@dataclass
class ValidationResult:
    score: int
    total_rows: int
    total_columns: int
    issues: list[Issue]
    counts: dict[str, int]
    columns: list[ColumnProfile]
    lossy_rows: list[int]
    summary: str


def parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    """解析 CSV，支持引号与转义"""
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    data = rows[1:]
    # 补齐列数
    for r in data:
        while len(r) < len(headers):
            r.append("")
    return headers, data


def check_date(raw: str) -> tuple[bool, str | None]:
    """检查日期是否真实存在"""
    s = raw.strip()
    if not s:
        return True, None

    y = mo = d = 0
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$", s)
        if m:
            y, mo, d = int(m.group(3)), int(m.group(1)), int(m.group(2))
        else:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
            if m:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                return False, "unparseable date format"

    if not (1 <= mo <= 12):
        return False, f"month {mo} is invalid"

    # 该月的天数
    import calendar
    try:
        days = calendar.monthrange(y, mo)[1]
    except ValueError:
        return False, f"year {y} is invalid"
    if not (1 <= d <= days):
        return False, f"day {d} does not exist in {y}-{mo:02d}"
    return True, None


def parse_money(raw: str) -> tuple[bool, float | None]:
    """解析金额：$1,250.00 / (1,300) / 1.2k / 15%"""
    s = raw.strip()
    if not s:
        return True, None

    negative = bool(re.match(r"^\(.*\)$", s))
    t = re.sub(r"^\(|\)$", "", s)
    t = re.sub(r"[$€£¥,\s]", "", t)

    mult = 1.0
    if re.search(r"k$", t, re.I):
        mult = 1000.0
        t = t[:-1]
    elif re.search(r"m$", t, re.I):
        mult = 1e6
        t = t[:-1]

    if not re.match(r"^-?\d*\.?\d+$", t):
        return False, None
    v = float(t) * mult
    return True, (-v if negative else v)


def is_placeholder(v: str) -> bool:
    return v.strip().lower() in PLACEHOLDERS


def infer_column_type(values: list[str]) -> str:
    non_empty = [v for v in values if v.strip() and not is_placeholder(v)]
    if not non_empty:
        return "empty"

    num = date = money = 0
    for v in non_empty:
        ok_d, _ = check_date(v)
        if ok_d:
            date += 1
        ok_m, _ = parse_money(v)
        if ok_m and re.search(r"[\d$€£¥]", v):
            money += 1
            num += 1
        elif re.match(r"^-?\d*\.?\d+$", v.strip()):
            num += 1

    n = len(non_empty)
    # 日期判定：需要「能解析成日期格式」的比例够高（不看是否真实存在）
    # 这样 2026-02-30 这种坏值也归入 date 列，才能被检出
    date_format = 0
    for v in non_empty:
        if re.match(r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{8})$", v.strip()):
            date_format += 1
    if date_format / n > 0.7:
        return "date"
    if re.search(r"[$€£¥,]", "".join(non_empty)) and money / n > 0.8:
        return "money"
    if num / n > 0.8:
        return "number"
    if 0.2 < num / n < 0.8:
        return "mixed"
    return "string"


def validate(
    csv_text: str,
    required_columns: list[str] | None = None,
    key_column: str | None = None,
) -> ValidationResult:
    """主校验函数（与 TS 版行为一致）"""
    headers, rows = parse_csv(csv_text)
    issues: list[Issue] = []

    if not headers:
        return ValidationResult(
            score=100, total_rows=0, total_columns=0, issues=[],
            counts={"high": 0, "mid": 0, "low": 0}, columns=[],
            lossy_rows=[], summary="No data",
        )

    # ---------- 列画像 ----------
    columns: list[ColumnProfile] = []
    for idx, name in enumerate(headers):
        values = [(r[idx] if idx < len(r) else "") for r in rows]
        non_empty = [v for v in values if v.strip() and not is_placeholder(v)]
        uniq = {v.strip() for v in non_empty}
        inferred = infer_column_type(values)

        mismatch = 0
        if inferred == "number":
            for v in non_empty:
                if not re.match(r"^-?\d*\.?\d+$", v.strip()):
                    ok, _ = parse_money(v)
                    if not ok:
                        mismatch += 1
        elif inferred == "date":
            for v in non_empty:
                ok, _ = check_date(v)
                if not ok:
                    mismatch += 1

        columns.append(ColumnProfile(
            name=name, index=idx, inferred_type=inferred,
            total_rows=len(rows),
            empty_count=len(values) - len(non_empty),
            unique_count=len(uniq),
            type_mismatch_rate=(mismatch / len(non_empty)) if non_empty else 0.0,
            samples=non_empty[:3],
        ))

    # ---------- 1. 完全重复行 ----------
    seen_rows: dict[str, int] = {}
    for i, r in enumerate(rows):
        key = "\x01".join(r)
        if key in seen_rows:
            issues.append(Issue(
                type="duplicate_row", severity="high",
                column="(whole row)", row=i + 2,
                value=",".join(r[:3]),
                reason=f"identical to row {seen_rows[key] + 2}",
                fix="remove the duplicate row, or add a unique identifier",
            ))
        else:
            seen_rows[key] = i

    # ---------- 2. 主键重复 ----------
    if key_column:
        ki = next((i for i, h in enumerate(headers)
                   if h.lower() == key_column.lower()), -1)
        if ki >= 0:
            seen_key: dict[str, int] = {}
            for i, r in enumerate(rows):
                v = (r[ki] if ki < len(r) else "").strip().lower()
                # 归一化：去字母前缀的前导零（A-001 == A-1），保留纯数字的处理
                _m = re.match(r"^([a-z]+)-?(\d+)$", v)
                if _m:
                    v = f"{_m.group(1)}-{int(_m.group(2))}"
                else:
                    v = re.sub(r"^0+(?=\d)", "", v)
                if not v:
                    continue
                if v in seen_key:
                    issues.append(Issue(
                        type="duplicate_key", severity="high",
                        column=headers[ki], row=i + 2,
                        value=r[ki] if ki < len(r) else "",
                        reason=f"key repeats (first seen at row {seen_key[v] + 2})",
                        fix="a join on this column will produce duplicate output rows",
                    ))
                else:
                    seen_key[v] = i

    # ---------- 3. 占位符 ----------
    for i, r in enumerate(rows):
        for ci, v in enumerate(r):
            if ci < len(headers) and is_placeholder(v):
                issues.append(Issue(
                    type="placeholder_value", severity="mid",
                    column=headers[ci], row=i + 2, value=v,
                    reason=f'"{v.strip()}" looks like a value but is a placeholder',
                    fix="replace with empty string, or fill the real value",
                ))

    # ---------- 4. 必填列为空 ----------
    for rc in required_columns or []:
        ci = next((i for i, h in enumerate(headers)
                   if h.lower() == rc.lower()), -1)
        if ci < 0:
            continue
        for i, r in enumerate(rows):
            v = (r[ci] if ci < len(r) else "").strip()
            if not v or is_placeholder(v):
                issues.append(Issue(
                    type="empty_required", severity="high",
                    column=headers[ci], row=i + 2, value=v,
                    reason="required column is empty",
                    fix="fill it, or the row may be silently dropped downstream",
                ))

    # ---------- 5. 按列类型检查 ----------
    for col in columns:
        if col.inferred_type in ("empty", "string"):
            continue
        for i, r in enumerate(rows):
            v = (r[col.index] if col.index < len(r) else "").strip()
            if not v or is_placeholder(v):
                continue

            if col.inferred_type == "date":
                ok, reason = check_date(v)
                if not ok:
                    is_impossible = "does not exist" in (reason or "")
                    issues.append(Issue(
                        type="impossible_date" if is_impossible else "invalid_date",
                        severity="high", column=col.name, row=i + 2, value=v,
                        reason=reason or "unparseable date",
                        fix="normalize to YYYY-MM-DD, then re-validate",
                    ))
            elif col.inferred_type in ("number", "money"):
                is_num = bool(re.match(r"^-?\d*\.?\d+$", v))
                if not is_num:
                    ok_m, _ = parse_money(v)
                    if not ok_m:
                        issues.append(Issue(
                            type="invalid_number", severity="high",
                            column=col.name, row=i + 2, value=v,
                            reason=f"column is {col.inferred_type} but this value is not",
                            fix="this row will fail a numeric cast and may vanish",
                        ))

    # ---------- 6. 空白 ----------
    for i, r in enumerate(rows):
        for ci, v in enumerate(r):
            if ci < len(headers) and v != v.strip() and v.strip():
                issues.append(Issue(
                    type="whitespace", severity="low",
                    column=headers[ci], row=i + 2, value=v,
                    reason="leading or trailing whitespace",
                    fix="breaks string joins and unique constraints",
                ))

    # ---------- 统计 ----------
    counts = {"high": 0, "mid": 0, "low": 0}
    for it in issues:
        counts[it.severity] += 1

    lossy = sorted({it.row for it in issues if it.severity == "high"})
    row_count = max(len(rows), 1)
    penalty = (
        counts["high"] * 100 / row_count * 0.8
        + counts["mid"] * 100 / row_count * 0.35
        + counts["low"] * 100 / row_count * 0.1
    )
    score = max(0, min(100, round(100 - penalty)))

    if score >= 95:
        summary = "Clean. Nothing would break downstream."
    elif score >= 80:
        summary = "Minor issues. Safe to process with care."
    elif score >= 60:
        summary = "Real problems. Some rows would be silently lost."
    else:
        summary = "Serious problems. Do not load this as-is."

    return ValidationResult(
        score=score, total_rows=len(rows), total_columns=len(headers),
        issues=issues, counts=counts, columns=columns,
        lossy_rows=lossy, summary=summary,
    )


def to_dict(r: ValidationResult) -> dict[str, Any]:
    """转成 JSON 可序列化结构"""
    return {
        "score": r.score,
        "total_rows": r.total_rows,
        "total_columns": r.total_columns,
        "counts": r.counts,
        "lossy_rows": r.lossy_rows,
        "summary": r.summary,
        "issues": [asdict(i) for i in r.issues],
        "columns": [asdict(c) for c in r.columns],
    }


def to_markdown(r: ValidationResult) -> str:
    lines = [
        "# Data Health Report",
        "",
        f"**Score: {r.score}/100** — {r.summary}",
        "",
        f"- Rows: {r.total_rows}",
        f"- Columns: {r.total_columns}",
        f"- High severity: {r.counts['high']}",
        f"- Rows that would be lost: {len(r.lossy_rows)}",
        "",
    ]
    if r.issues:
        lines.append("## Issues")
        lines.append("")
        lines.append("| Row | Column | Value | Problem | Fix |")
        lines.append("|---|---|---|---|---|")
        for it in r.issues[:100]:
            lines.append(
                f"| {it.row} | {it.column} | `{it.value[:20]}` | {it.reason} | {it.fix} |"
            )
        if len(r.issues) > 100:
            lines.append(f"| … | | | {len(r.issues) - 100} more | |")
    return "\n".join(lines)
