"""Compare Cube results with reference results (golden numbers or BigQuery rows)."""
from __future__ import annotations

import datetime as dt
import decimal
import re
from typing import Any

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


def to_number(value: Any) -> float | None:
    """Cube returns numbers as strings; BigQuery returns Decimal/int/float. Normalise to float."""
    if value is None or value == "":
        return None
    if isinstance(value, decimal.Decimal):
        return float(value)
    return float(value)


def to_key(value: Any, time_precision: str = "date") -> Any:
    """Normalise a grouping key so a Cube value and a BigQuery value compare equal.

    Times: Cube returns '2026-10-01T00:00:00.000', BigQuery returns date/datetime objects.
    With time_precision='date' both become '2026-10-01'.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        value = value.isoformat()
    if isinstance(value, str) and _ISO.match(value):
        return value[:10] if time_precision == "date" else value[:19].replace(" ", "T")
    if isinstance(value, (int, float, decimal.Decimal)):
        return float(value)
    return value


def close(a: float | None, b: float | None, abs_tol: float, rel_tol: float) -> bool:
    """Equal within max(abs_tol, rel_tol * |b|). Two NULLs are equal; NULL vs 0 is not."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(abs_tol, rel_tol * abs(b))


def compare_rowsets(cube_rows: list[dict[str, Any]], ref_rows: list[dict[str, Any]],
                    keys: dict[str, str], values: dict[str, str],
                    abs_tol: float, rel_tol: float,
                    time_precision: str = "date") -> list[str]:
    """Match rows on `keys` and compare `values`.

    Args:
        cube_rows: Rows from Cube /load (keys are member names, e.g. 'orders.state').
        ref_rows: Rows from BigQuery (keys are column names).
        keys: Cube member -> BigQuery column, for the grouping columns.
        values: Cube member -> BigQuery column, for the measures.

    Returns:
        Human-readable mismatch descriptions (empty means the two agree).
    """
    def index(rows: list[dict[str, Any]], colmap: dict[str, str], use_ref_names: bool) -> dict:
        out: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            k = tuple(to_key(row[ref if use_ref_names else cube], time_precision)
                      for cube, ref in colmap.items())
            if k in out:
                raise AssertionError(f"duplicate key {k} — the comparison keys are not unique")
            out[k] = row
        return out

    cube_ix = index(cube_rows, keys, use_ref_names=False)
    ref_ix = index(ref_rows, keys, use_ref_names=True)
    problems: list[str] = []
    for k in sorted(set(ref_ix) - set(cube_ix), key=str):
        problems.append(f"{k}: in BigQuery but missing from Cube")
    for k in sorted(set(cube_ix) - set(ref_ix), key=str):
        problems.append(f"{k}: in Cube but missing from BigQuery")
    for k in sorted(set(cube_ix) & set(ref_ix), key=str):
        for cube_member, ref_col in values.items():
            a = to_number(cube_ix[k].get(cube_member))
            b = to_number(ref_ix[k].get(ref_col))
            if not close(a, b, abs_tol, rel_tol):
                problems.append(f"{k} {cube_member}: Cube={a} BigQuery={b}")
    return problems
