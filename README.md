# dvq — Data Validator Quick

Check a CSV for the data problems that break pipelines silently.

```bash
pip install dvq
```

## Why

Every data pipeline assumes the input is clean. It isn't.

`dvq` finds the failures that **do not throw errors**:

- A date that parses but doesn't exist (`2026-02-30` passes most parsers)
- A numeric column with `"$1,250.00"` — `float()` raises, `NaN` slips through
- The same key twice — your join silently duplicates every row
- `N/A` where you expected empty — passes a null check, poisons the join
- Trailing whitespace — `"A-1001 "` ≠ `"A-1001"`, so `Merge` returns nothing

## Usage

```bash
dvq check orders.csv
dvq check orders.csv --key order_id --required email,amount
dvq check orders.csv --json
dvq check orders.csv --fix cleaned.csv
```

```
====================================================================
  Data Health Score: 48/100
  [#########...........]
====================================================================
  Serious problems. Do not load this as-is.

  Rows     7
  High     4
  At risk  3 rows would be lost

  Issues (first 20)
  ----------------------------------------------------------------
    !! row 4    order_id         key repeats (first seen at row 3)
    !! row 8    date             month 13 is invalid
    !! row 8    email            required column is empty
    !  row 5    email            "N/A" looks like a value but is a placeholder
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean, or below the thresholds you set |
| 1 | Threshold crossed (`--min-score` / `--fail-on`) |
| 2 | File error |
| 3 | Free tier limit reached |

Use it in CI:

```yaml
- run: dvq check data.csv --fail-on high
```

## As a library

```python
from dvq_core.validator import validate

result = validate(csv_text, key_column="order_id")
print(result.score, result.counts)
for issue in result.issues:
    print(issue.row, issue.column, issue.reason, issue.fix)
```

## Pro

Free: single-table checks, 100 runs/day, 10k rows.

Pro ($49 once): cross-table consistency, alert webhooks, no limits.
https://checkout.dodopayments.com/buy/pdt_0Nq62Ecr9PnrPukjAEhaM?ref=pypi-cli

## License

MIT
