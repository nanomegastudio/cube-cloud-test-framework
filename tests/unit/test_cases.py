"""Cases run in any environment (dataset variables) and any day (relative date ranges)."""
from __future__ import annotations

import datetime as dt
import textwrap

import pytest

from semtest import cases, config

pytestmark = pytest.mark.unit

TUE_17_NOV = dt.date(2026, 11, 17)   # a Tuesday: the last complete ISO week is Mon 9 to Sun 15


@pytest.mark.parametrize("spec,expected", [
    ("yesterday", ["2026-11-16", "2026-11-16"]),
    ("last_7_complete_days", ["2026-11-10", "2026-11-16"]),
    ("last_1_complete_days", ["2026-11-16", "2026-11-16"]),
    ("last_complete_week", ["2026-11-09", "2026-11-15"]),
    ("last_complete_month", ["2026-10-01", "2026-10-31"]),
    ("last_complete_quarter", ["2026-07-01", "2026-09-30"]),
    ("last_complete_year", ["2025-01-01", "2025-12-31"]),
    (["2026-01-01", "2026-01-31"], ["2026-01-01", "2026-01-31"]),
    ([dt.date(2026, 1, 1), dt.date(2026, 1, 31)], ["2026-01-01", "2026-01-31"]),
])
def test_resolve_range(spec, expected) -> None:
    assert cases.resolve_range(spec, TUE_17_NOV) == expected


def test_complete_periods_never_include_today() -> None:
    first_of_month = dt.date(2026, 12, 1)
    assert cases.resolve_range("last_complete_month", first_of_month) == ["2026-11-01", "2026-11-30"]
    sunday = dt.date(2026, 11, 22)                      # the week of 16 to 22 is not over yet
    assert cases.resolve_range("last_complete_week", sunday) == ["2026-11-09", "2026-11-15"]
    jan_1 = dt.date(2027, 1, 1)
    assert cases.resolve_range("last_complete_quarter", jan_1) == ["2026-10-01", "2026-12-31"]


def test_unknown_range_is_an_error() -> None:
    with pytest.raises(config.ConfigError, match="unknown dateRange"):
        cases.resolve_range("last_fortnight", TUE_17_NOV)


def test_as_of_pins_today(monkeypatch) -> None:
    monkeypatch.setenv("SEMTEST_AS_OF", "2026-03-15")
    assert cases.today() == dt.date(2026, 3, 15)


# ------------------------------------------------------------------ SQL variables

@pytest.fixture
def suite(tmp_path, monkeypatch):
    for var in ("SEMTEST_VAR_GOLD_DATASET", "SEMTEST_VAR_PROJECT", "SEMTEST_AS_OF"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path / "no.env")
    monkeypatch.setenv("SEMTEST_CONFIG_DIR", str(tmp_path))
    (tmp_path / "suite.yml").write_text(textwrap.dedent("""
        timezone: Australia/Sydney
        sql_vars: {project: acme, gold_dataset: gold}
    """))


CASE = {
    "id": "by_state",
    "cube_query": {
        "measures": ["revenue.net_revenue"],
        "timeDimensions": [{"dimension": "revenue.order_date", "granularity": "month",
                            "dateRange": "last_complete_quarter"}],
        "timezone": "Australia/Sydney",
    },
    "bigquery_sql": "SELECT 1 FROM `${project}.${gold_dataset}.fct_orders` "
                    "WHERE d BETWEEN '${date_from}' AND '${date_to}'",
    "freshness": {"time_dimension": "revenue.order_date",
                  "bigquery_max_sql": "SELECT MAX(d) FROM `${project}.${gold_dataset}.fct_orders`"},
}


def test_prepare_fills_prod_dataset_and_dates(suite) -> None:
    got = cases.prepare(CASE, on=TUE_17_NOV)
    assert got["cube_query"]["timeDimensions"][0]["dateRange"] == ["2026-07-01", "2026-09-30"]
    assert got["bigquery_sql"] == ("SELECT 1 FROM `acme.gold.fct_orders` "
                                   "WHERE d BETWEEN '2026-07-01' AND '2026-09-30'")
    assert got["freshness"]["bigquery_max_sql"].endswith("`acme.gold.fct_orders`")
    assert CASE["cube_query"]["timeDimensions"][0]["dateRange"] == "last_complete_quarter"  # not mutated


def test_preprod_override_points_reference_sql_at_staging(suite, monkeypatch) -> None:
    """The reason for variables: pre-prod Cube reads gold_staging, so the reference must too."""
    monkeypatch.setenv("SEMTEST_VAR_GOLD_DATASET", "gold_staging")
    got = cases.prepare(CASE, on=TUE_17_NOV)
    assert "`acme.gold_staging.fct_orders`" in got["bigquery_sql"]
    assert "`acme.gold_staging.fct_orders`" in got["freshness"]["bigquery_max_sql"]


def test_undefined_variable_is_an_error_not_left_in_sql(suite) -> None:
    case = {**CASE, "bigquery_sql": "SELECT 1 FROM `${project}.${silver_dataset}.t`"}
    with pytest.raises(config.ConfigError, match=r"\$\{silver_dataset\}"):
        cases.prepare(case, on=TUE_17_NOV)


def test_fixed_golden_case_is_unchanged(suite) -> None:
    golden = {"id": "oct_close", "expected": 4210000.0, "cube_query": {
        "measures": ["revenue.net_revenue"],
        "timeDimensions": [{"dimension": "revenue.order_date",
                            "dateRange": [dt.date(2026, 10, 1), dt.date(2026, 10, 31)]}]}}
    got = cases.prepare(golden, on=TUE_17_NOV)
    assert got["cube_query"]["timeDimensions"][0]["dateRange"] == ["2026-10-01", "2026-10-31"]


def test_shipped_example_cases_all_prepare() -> None:
    """Every case in config/ resolves with the bundled suite.yml (catches typos in examples)."""
    for case in config.parity_cases() + config.rollup_cases():
        cases.prepare(case, on=TUE_17_NOV)
