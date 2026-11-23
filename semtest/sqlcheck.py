"""Read row-filter predicates out of the SQL Cube generates (/v1/sql).

The row-filter check used to look for each allowed value ANYWHERE in the SQL text and params, so a
value like `1` or `A` matched trivially, a filter on the wrong column passed, and a filter that
also let other values through passed. This module finds the predicates on the filter COLUMN and
returns exactly the values they bind, so the test can require "this column, these values, no
others".

Handles what Cube emits for an access-policy row filter or a `filters` member:
    `base_orders`.state = ?            (BigQuery, Snowflake: ? placeholders, values in params)
    "base_orders"."state" IN (?, ?)
    base_orders.state = $1             (Postgres-style numbered placeholders)
    base_orders.state = 'NSW'          (inlined literal)
It does not try to understand arbitrary SQL. If a predicate takes another shape (a function
around the column, a sub-select), no predicate is found and the test fails loudly rather than
passing; set the rule's `sql_column` or extend this parser.
"""
from __future__ import annotations

import re
from typing import Any

_QUOTED_IDENT = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_VALUE = r"(?:\?|\$\d+|'(?:[^']|'')*'|-?\d+(?:\.\d+)?)"


def _column_pattern(column: str) -> re.Pattern[str]:
    """`<optional qualifier>.<column>` (any quoting) followed by `=` or `IN (...)`."""
    col = re.escape(column)
    target = rf'(?:`{col}`|"{col}"|\[{col}\]|(?<![\w$]){col}(?![\w$]))'
    return re.compile(
        rf"(?:{_QUOTED_IDENT}\s*\.\s*)*{target}\s*"
        rf"(?:(?P<eq>=)\s*(?P<one>{_VALUE})"
        rf"|(?P<in>\bIN\b)\s*\(\s*(?P<many>{_VALUE}(?:\s*,\s*{_VALUE})*)\s*\))",
        re.IGNORECASE,
    )


def _qmarks_before(sql: str, end: int) -> int:
    """Number of `?` placeholders in sql[:end], ignoring any inside quotes."""
    count, quote = 0, None
    for ch in sql[:end]:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'`\"":
            quote = ch
        elif ch == "?":
            count += 1
    return count


def _resolve(token: str, qmark_index: int, params: list[Any]) -> tuple[Any, int]:
    """The value a placeholder or literal stands for, and the updated `?` counter."""
    if token == "?":
        if qmark_index >= len(params):
            raise ValueError(f"SQL has more ? placeholders than params ({len(params)})")
        return params[qmark_index], qmark_index + 1
    if token.startswith("$"):
        return params[int(token[1:]) - 1], qmark_index
    if token.startswith("'"):
        return token[1:-1].replace("''", "'"), qmark_index
    return token, qmark_index


def filter_values(sql: str, params: list[Any] | None, column: str) -> list[Any] | None:
    """Every value bound to an `=`/`IN` predicate on `column`, or None if there is none.

    Args:
        sql: The SQL text from /v1/sql (`sql[0]`).
        params: The bound parameters (`sql[1]`), in placeholder order.
        column: The column the row filter should constrain (unquoted, e.g. "state").
    """
    params = list(params or [])
    found: list[Any] = []
    matched = False
    for match in _column_pattern(column).finditer(sql):
        matched = True
        tokens = ([match.group("one")] if match.group("eq")
                  else re.findall(_VALUE, match.group("many")))
        index = _qmarks_before(sql, match.start("one") if match.group("eq")
                               else match.start("many"))
        for token in tokens:
            value, index = _resolve(token, index, params)
            found.append(value)
    return found if matched else None


def row_filter_problem(sql: str, params: list[Any] | None, column: str,
                       allowed: list[Any]) -> str | None:
    """Why the SQL does not restrict `column` to exactly `allowed`, or None if it does."""
    values = filter_values(sql, params, column)
    if values is None:
        return f"no `{column} = …` or `{column} IN (…)` predicate in the generated SQL"
    got = {str(v) for v in values}
    want = {str(v) for v in allowed}
    extra, missing = sorted(got - want), sorted(want - got)
    if extra:
        return f"the filter on {column} also allows {extra} (allowed: {sorted(want)})"
    if missing:
        return f"the filter on {column} does not include {missing} (it binds {sorted(got)})"
    return None
