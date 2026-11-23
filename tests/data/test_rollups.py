"""Rollup tier: pre-aggregations are used, built, correct and fresh.

Routing alone (the rollup named in the generated SQL) does not prove the rollup exists or holds
the right numbers. Each case in config/rollup_cases.yml is checked four ways.
"""
from __future__ import annotations

import json

import pytest

from semtest import cases, config
from semtest.compare import compare_rowsets
from semtest.freshness import freshness_granularity, freshness_problems, freshness_query

pytestmark = pytest.mark.rollup

CASES = config.rollup_cases()
IDS = [c["id"] for c in CASES]


def _routed_names(sql_block) -> str:
    """Everything /v1/sql says about matched pre-aggregations, as one searchable string."""
    return json.dumps(sql_block.get("preAggregations") or [], default=str) + " " + sql_block["sql"][0]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_query_is_routed_to_the_rollup(cube, personas, case) -> None:
    """No cost: /v1/sql shows the match without running anything."""
    case = cases.prepare(case)
    block = cube.sql(case["cube_query"], personas[case["persona"]]["claims"])
    assert case["pre_aggregation"] in _routed_names(block), (
        f"{case['id']}: not matched to {case['pre_aggregation']} — it would hit BigQuery.\n"
        f"preAggregations={block.get('preAggregations')}")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_rollup_is_materialised_and_served(cube, personas, case) -> None:
    """The rollup was actually built and the answer came from it."""
    case = cases.prepare(case)
    resp = cube.load(case["cube_query"], personas[case["persona"]]["claims"])
    used = json.dumps(resp.get("usedPreAggregations") or {})
    assert case["pre_aggregation"] in used, (
        f"{case['id']}: answered, but not from {case['pre_aggregation']} "
        f"(usedPreAggregations={used[:300]})")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_rollup_values_match_bigquery(cube, personas, bq, tol, case, dq) -> None:
    case = cases.prepare(case)
    rows = cube.rows_all(case["cube_query"], personas[case["persona"]]["claims"])
    ref = bq.query(case["bigquery_sql"])
    problems = compare_rowsets(rows, ref, case.get("keys") or {}, case["values"], *tol)
    dq.observe(observed={"mismatches": len(problems), "cube_rows": len(rows)},
               expected={"mismatches": 0, "bigquery_rows": len(ref)})
    assert not problems, f"{case['id']}: rollup disagrees with BigQuery\n  " + "\n  ".join(
        problems[:25])


@pytest.mark.parametrize("case", [c for c in CASES if c.get("freshness")],
                         ids=[c["id"] for c in CASES if c.get("freshness")])
def test_rollup_is_fresh(cube, personas, bq, case, dq) -> None:
    """The rollup itself (not BigQuery behind it) holds the latest period the source allows.

    Asked at the rollup's own granularity and required to be served by it; see
    semtest/freshness.py for why a day-level question on a monthly rollup proved nothing.
    """
    case = cases.prepare(case)
    query = freshness_query(case, freshness_granularity(case))
    resp = cube.load(query, personas[case["persona"]]["claims"])
    source_latest = bq.scalar(case["freshness"]["bigquery_max_sql"])
    problems = freshness_problems(case, resp, source_latest)
    td = case["freshness"]["time_dimension"]
    gran = freshness_granularity(case)
    latest = (resp.get("data") or [{}])[0].get(f"{td}.{gran}")
    dq.observe(observed={"rollup_latest": latest, "granularity": gran},
               expected={"source_latest": source_latest,
                         "max_lag_days": case["freshness"].get("max_lag_days", 1)})
    assert not problems, f"{case['id']}:\n  " + "\n  ".join(problems)
