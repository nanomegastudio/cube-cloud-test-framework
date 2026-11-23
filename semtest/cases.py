"""Make config cases portable across environments and across time.

Two things in a case used to be hard-coded:

1. The dataset. Reference SQL said `your-project.gold.fct_orders`, so it could only check prod.
   With a pre-prod Cube reading `gold_staging`, that compared staging numbers with prod tables.
   Now SQL uses `${name}` variables from suite.yml › sql_vars, each overridable per environment
   with SEMTEST_VAR_<NAME> (e.g. SEMTEST_VAR_GOLD_DATASET=gold_staging in pre-prod).

2. The dates. Fixed ranges like [2026-10-01, 2026-10-07] age: once that week is outside the data
   (or outside a staging dataset that only holds recent days), checks fail or test nothing.
   A dateRange may now be a relative name, resolved against "today" in the query's time zone:

       yesterday · last_N_complete_days (e.g. last_7_complete_days) · last_complete_week
       last_complete_month · last_complete_quarter · last_complete_year

   "Complete" periods end yesterday or earlier, so a run never compares a half-loaded today.
   The resolved range is also available to that case's SQL as ${date_from} and ${date_to}.
   SEMTEST_AS_OF=YYYY-MM-DD pins "today" to re-run a past day.

Golden numbers should keep fixed dates: a finance close is for one period.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from semtest import config

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LAST_N_DAYS = re.compile(r"^last_(\d+)_complete_days$")
RELATIVE_RANGES = ("yesterday", "last_N_complete_days", "last_complete_week",
                   "last_complete_month", "last_complete_quarter", "last_complete_year")


# ------------------------------------------------------------------ dates

def today(timezone: str | None = None) -> dt.date:
    """Today in `timezone` (default suite.yml › timezone, else UTC), or SEMTEST_AS_OF if set."""
    pinned = config.env("SEMTEST_AS_OF")
    if pinned:
        return dt.date.fromisoformat(pinned)
    tz = timezone or config.suite().get("timezone") or "UTC"
    return dt.datetime.now(ZoneInfo(tz)).date()


def resolve_range(spec: Any, on: dt.date) -> list[str] | None:
    """A dateRange as Cube wants it: [from, to] ISO dates. Lists pass through unchanged.

    Args:
        spec: [from, to] (dates or strings), a relative name (see RELATIVE_RANGES), or None.
        on: The date that counts as "today".
    """
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)):
        return [str(v) for v in spec]
    name = str(spec).strip().lower()
    yesterday = on - dt.timedelta(days=1)
    if name == "yesterday":
        start, end = yesterday, yesterday
    elif m := _LAST_N_DAYS.match(name):
        n = int(m.group(1))
        if n < 1:
            raise config.ConfigError(f"dateRange {spec!r}: N must be at least 1")
        start, end = yesterday - dt.timedelta(days=n - 1), yesterday
    elif name == "last_complete_week":                      # ISO week, Monday to Sunday
        end = on - dt.timedelta(days=on.weekday() + 1)
        start = end - dt.timedelta(days=6)
    elif name == "last_complete_month":
        end = on.replace(day=1) - dt.timedelta(days=1)
        start = end.replace(day=1)
    elif name == "last_complete_quarter":
        this_q = on.replace(month=3 * ((on.month - 1) // 3) + 1, day=1)
        end = this_q - dt.timedelta(days=1)
        start = end.replace(month=3 * ((end.month - 1) // 3) + 1, day=1)
    elif name == "last_complete_year":
        start, end = dt.date(on.year - 1, 1, 1), dt.date(on.year - 1, 12, 31)
    else:
        raise config.ConfigError(
            f"unknown dateRange {spec!r}; use [from, to] or one of {', '.join(RELATIVE_RANGES)}")
    return [start.isoformat(), end.isoformat()]


# ------------------------------------------------------------------ SQL variables

def sql_vars(extra: dict[str, Any] | None = None) -> dict[str, str]:
    """suite.yml › sql_vars, overridden by SEMTEST_VAR_<NAME> env vars, plus `extra`."""
    out = {str(k): str(v) for k, v in (config.suite().get("sql_vars") or {}).items()}
    for key in list(out):
        override = config.env(f"SEMTEST_VAR_{key.upper()}")
        if override:
            out[key] = override
    out.update({k: str(v) for k, v in (extra or {}).items()})
    return out


def render_sql(sql: str, variables: dict[str, str]) -> str:
    """Replace ${name}; an unknown name is an error, never left in the SQL."""
    missing = sorted({m.group(1) for m in _VAR.finditer(sql)} - set(variables))
    if missing:
        raise config.ConfigError(
            f"reference SQL uses {', '.join('${' + n + '}' for n in missing)} but suite.yml › "
            f"sql_vars does not define {'it' if len(missing) == 1 else 'them'}")
    return _VAR.sub(lambda m: variables[m.group(1)], sql)


# ------------------------------------------------------------------ cases

def prepare(case: dict[str, Any], on: dt.date | None = None) -> dict[str, Any]:
    """A copy of `case` ready to run: relative dateRanges resolved, SQL variables filled in.

    The first time dimension with a dateRange supplies ${date_from} / ${date_to}.
    """
    out = copy.deepcopy(case)
    query = out.get("cube_query") or {}
    when = on or today(query.get("timezone"))
    extra: dict[str, str] = {}
    for td in query.get("timeDimensions") or []:
        if "dateRange" in td:
            td["dateRange"] = resolve_range(td["dateRange"], when)
            extra.setdefault("date_from", td["dateRange"][0])
            extra.setdefault("date_to", td["dateRange"][1])
    variables = sql_vars(extra)
    if out.get("bigquery_sql"):
        out["bigquery_sql"] = render_sql(out["bigquery_sql"], variables)
    fresh = out.get("freshness") or {}
    if fresh.get("bigquery_max_sql"):
        fresh["bigquery_max_sql"] = render_sql(fresh["bigquery_max_sql"], variables)
    return out


def smoke_window(suite_cfg: dict[str, Any]) -> list[str] | None:
    """suite.yml › smoke_date_range, resolved (relative names allowed)."""
    return resolve_range(suite_cfg.get("smoke_date_range"), today())
