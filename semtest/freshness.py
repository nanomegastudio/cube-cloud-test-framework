"""Rollup freshness: is the newest period a rollup serves as new as the source allows?

The old check asked for the latest DAY. A monthly rollup cannot answer a daily query, so Cube sent
it straight to BigQuery and the test measured the source's freshness, not the rollup's: it passed
on a stale rollup. The check now:

1. asks at the rollup's own granularity (the case's, or `freshness.granularity`),
2. fails unless that answer was served by the named rollup (`usedPreAggregations`),
3. compares periods: the rollup must hold the period containing (source latest - max_lag_days),
4. optionally (`max_refresh_age_hours`) checks when the rollup was last built, which is the only
   way to catch a coarse rollup that is stale WITHIN its current period.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from semtest.compare import to_key

GRANULARITIES = ("day", "week", "month", "quarter", "year")
_SUB_DAY = ("second", "minute", "hour")


def freshness_granularity(case: dict[str, Any]) -> str:
    """`freshness.granularity`, else the case query's granularity on the time dimension, else day."""
    f = case["freshness"]
    if f.get("granularity"):
        return f["granularity"]
    for td in case["cube_query"].get("timeDimensions") or []:
        if td.get("dimension") == f["time_dimension"] and td.get("granularity"):
            return td["granularity"]
    return "day"


def period_start(day: dt.date, granularity: str) -> dt.date:
    """First day of the period (ISO week, calendar month/quarter/year) that contains `day`."""
    if granularity in _SUB_DAY or granularity == "day":
        return day
    if granularity == "week":
        return day - dt.timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    if granularity == "quarter":
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    if granularity == "year":
        return day.replace(month=1, day=1)
    raise ValueError(f"unknown granularity {granularity!r}; use one of {GRANULARITIES}")


def freshness_query(case: dict[str, Any], granularity: str) -> dict[str, Any]:
    """Latest period of the case's first measure, at `granularity`, newest first."""
    td = case["freshness"]["time_dimension"]
    query: dict[str, Any] = {
        "measures": case["cube_query"]["measures"][:1],
        "timeDimensions": [{"dimension": td, "granularity": granularity}],
        "order": {td: "desc"},
        "limit": 1,
    }
    if case["cube_query"].get("timezone"):
        query["timezone"] = case["cube_query"]["timezone"]
    return query


def served_by(resp: dict[str, Any], pre_aggregation: str) -> bool:
    """True when /load's usedPreAggregations names the rollup (substring match, like routing)."""
    return pre_aggregation in json.dumps(resp.get("usedPreAggregations") or {}, default=str)


def last_refreshed(resp: dict[str, Any], pre_aggregation: str) -> dt.datetime | None:
    """When the serving rollup was built, from usedPreAggregations[*].lastUpdatedAt (epoch ms),
    falling back to the response's lastRefreshTime (ISO). None if Cube reported neither."""
    for name, info in (resp.get("usedPreAggregations") or {}).items():
        if pre_aggregation in name and isinstance(info, dict) and info.get("lastUpdatedAt"):
            return dt.datetime.fromtimestamp(int(info["lastUpdatedAt"]) / 1000, dt.timezone.utc)
    raw = resp.get("lastRefreshTime")
    if raw:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def freshness_problems(case: dict[str, Any], resp: dict[str, Any], source_latest: Any,
                       now: dt.datetime | None = None) -> list[str]:
    """Everything wrong with the rollup's freshness for this case (empty means fresh)."""
    f = case["freshness"]
    name = case["pre_aggregation"]
    gran = freshness_granularity(case)
    if not served_by(resp, name):
        return [f"the freshness query (granularity={gran}) was not served by {name} "
                f"(usedPreAggregations={json.dumps(resp.get('usedPreAggregations') or {})[:300]}), "
                f"so it would measure BigQuery, not the rollup. Set freshness.granularity to a "
                f"granularity the rollup can answer."]
    rows = resp.get("data") or []
    if not rows:
        return [f"{name} returned no rows at all"]

    problems = []
    key = f"{f['time_dimension']}.{gran}"
    cube_period = period_start(dt.date.fromisoformat(to_key(rows[0][key])), gran)
    src_day = dt.date.fromisoformat(to_key(source_latest))
    lag_days = int(f.get("max_lag_days", 1))
    required = period_start(src_day - dt.timedelta(days=lag_days), gran)
    if cube_period < required:
        problems.append(
            f"{name}'s latest {gran} is {cube_period}; BigQuery's latest day is {src_day}, so with "
            f"max_lag_days={lag_days} the rollup should hold the {gran} starting {required}. "
            f"Has the rollup refreshed since the last dbt run?")

    max_age = f.get("max_refresh_age_hours")
    if max_age is not None:
        built = last_refreshed(resp, name)
        now = now or dt.datetime.now(dt.timezone.utc)
        if built is None:
            problems.append(f"max_refresh_age_hours is set but Cube reported no lastUpdatedAt or "
                            f"lastRefreshTime for {name}")
        elif now - built > dt.timedelta(hours=float(max_age)):
            age = (now - built).total_seconds() / 3600
            problems.append(f"{name} was last built {built:%Y-%m-%d %H:%M} UTC, {age:.1f} hours "
                            f"ago (max_refresh_age_hours={max_age})")
    return problems
