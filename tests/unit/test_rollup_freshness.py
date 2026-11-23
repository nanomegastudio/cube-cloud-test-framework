"""Bug #1: rollup freshness must be measured on the rollup, at a granularity it can serve.

The old test asked a monthly rollup for the latest DAY. Real Cube answers that from BigQuery, so
the test checked the source and passed on a stale rollup. The fake here behaves like real Cube:
it only serves month-or-coarser queries from the rollup.
"""
from __future__ import annotations

import datetime as dt

import pytest

from semtest.freshness import freshness_granularity, freshness_problems, period_start
from tests.unit.fakes import fake_cube

pytestmark = pytest.mark.unit

CASE = {
    "id": "revenue_monthly_by_state",
    "persona": "ci_reader",
    "pre_aggregation": "revenue_by_state_monthly",
    "cube_query": {
        "measures": ["revenue.net_revenue"],
        "timeDimensions": [{"dimension": "revenue.order_date", "granularity": "month"}],
        "timezone": "Australia/Sydney",
    },
    "freshness": {"time_dimension": "revenue.order_date", "bigquery_max_sql": "SELECT 1",
                  "max_lag_days": 1},
}
ROLLUP = "prod_pre_aggregations.revenue_revenue_by_state_monthly_abc123"
SOURCE_LATEST = dt.date(2026, 10, 15)          # BigQuery has data to mid-October


def _cube_like(rollup_latest_month: str, source_latest_day: str, built_ms: int | None = None):
    """Serves month queries from a rollup that stops at `rollup_latest_month`; anything finer
    falls through to the (fresh) source, with no usedPreAggregations, as real Cube does."""
    def handler(query):
        td = query["timeDimensions"][0]
        if td["granularity"] in ("month", "quarter", "year"):
            info = {"targetTableName": ROLLUP}
            if built_ms is not None:
                info["lastUpdatedAt"] = built_ms
            key = f"{td['dimension']}.{td['granularity']}"
            return {"data": [{key: f"{rollup_latest_month}T00:00:00.000"}],
                    "usedPreAggregations": {ROLLUP: info}}
        key = f"{td['dimension']}.{td['granularity']}"
        return {"data": [{key: f"{source_latest_day}T00:00:00.000"}], "usedPreAggregations": {}}
    return handler


class _BQ:
    def scalar(self, sql):
        return SOURCE_LATEST


def _run_real_test(cube):
    from tests.data.test_rollups import test_rollup_is_fresh
    test_rollup_is_fresh(cube, {"ci_reader": {"claims": {}}}, _BQ(), CASE)


def test_stale_monthly_rollup_now_fails() -> None:
    """Rollup stuck at August while BigQuery has October. The old test passed this."""
    cube = fake_cube(_cube_like("2026-08-01", "2026-10-15"))
    with pytest.raises(AssertionError, match="latest month is 2026-08-01"):
        _run_real_test(cube)
    assert cube.session.queries[0]["timeDimensions"][0]["granularity"] == "month"


def test_fresh_monthly_rollup_passes() -> None:
    _run_real_test(fake_cube(_cube_like("2026-10-01", "2026-10-15")))


def test_query_answered_from_source_fails() -> None:
    """Forcing day granularity on a monthly rollup reproduces the old blind spot; it must fail."""
    case = {**CASE, "freshness": {**CASE["freshness"], "granularity": "day"}}
    resp = {"data": [{"revenue.order_date.day": "2026-10-15T00:00:00.000"}],
            "usedPreAggregations": {}}
    problems = freshness_problems(case, resp, SOURCE_LATEST)
    assert problems and "was not served by" in problems[0]


def test_lag_across_a_period_boundary_is_allowed() -> None:
    """Source latest is 1 Nov; with max_lag_days=1 October is still acceptable."""
    resp = {"data": [{"revenue.order_date.month": "2026-10-01T00:00:00.000"}],
            "usedPreAggregations": {ROLLUP: {}}}
    assert freshness_problems(CASE, resp, dt.date(2026, 11, 1)) == []


def test_refresh_age_catches_staleness_within_the_month() -> None:
    case = {**CASE, "freshness": {**CASE["freshness"], "max_refresh_age_hours": 26}}
    now = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
    three_days_ago = int((now - dt.timedelta(days=3)).timestamp() * 1000)
    resp = {"data": [{"revenue.order_date.month": "2026-10-01T00:00:00.000"}],
            "usedPreAggregations": {ROLLUP: {"lastUpdatedAt": three_days_ago}}}
    problems = freshness_problems(case, resp, SOURCE_LATEST, now=now)
    assert len(problems) == 1 and "72.0 hours ago" in problems[0]


def test_refresh_age_without_a_timestamp_fails_rather_than_passes() -> None:
    case = {**CASE, "freshness": {**CASE["freshness"], "max_refresh_age_hours": 26}}
    resp = {"data": [{"revenue.order_date.month": "2026-10-01T00:00:00.000"}],
            "usedPreAggregations": {ROLLUP: {}}}
    assert "reported no lastUpdatedAt" in freshness_problems(case, resp, SOURCE_LATEST)[0]


def test_granularity_defaults_to_the_case_query() -> None:
    assert freshness_granularity(CASE) == "month"


@pytest.mark.parametrize("gran,expected", [
    ("day", dt.date(2026, 8, 14)), ("week", dt.date(2026, 8, 10)),
    ("month", dt.date(2026, 8, 1)), ("quarter", dt.date(2026, 7, 1)),
    ("year", dt.date(2026, 1, 1)),
])
def test_period_start(gran, expected) -> None:
    assert period_start(dt.date(2026, 8, 14), gran) == expected
