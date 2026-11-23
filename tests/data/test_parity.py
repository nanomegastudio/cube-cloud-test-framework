"""Parity tier: the number Cube reports is the right number.

Cases live in config/parity_cases.yml. A case either names a golden number from an independent
source (strongest), or reference SQL run straight against BigQuery.
"""
from __future__ import annotations

import pytest

from semtest import cases, config
from semtest.compare import close, compare_rowsets, to_number

pytestmark = pytest.mark.parity

CASES = config.parity_cases()


@pytest.mark.contract
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case_is_well_formed(case) -> None:
    """Cheap config check (no network): every case is owned, and golden numbers cite a source."""
    assert case.get("owner"), f"{case['id']}: no owner"
    assert case.get("persona") in config.personas(), f"{case['id']}: unknown persona"
    assert ("expected" in case) != ("bigquery_sql" in case), (
        f"{case['id']}: set exactly one of `expected` or `bigquery_sql`")
    if "expected" in case:
        assert case.get("source"), f"{case['id']}: a golden number needs a `source`"
    else:
        assert case.get("values"), f"{case['id']}: `values` maps Cube measures to SQL columns"
    cases.prepare(case)  # unknown ${var} or dateRange name fails here, with no network needed


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_cube_matches_reference(cube, personas, tol, case, request, dq) -> None:
    case = cases.prepare(case)  # relative dates resolved, ${vars} filled for this environment
    claims = personas[case["persona"]]["claims"]
    rows = cube.rows_all(case["cube_query"], claims)
    abs_tol = float(case.get("abs_tol", tol[0]))
    rel_tol = float(case.get("rel_tol", tol[1]))

    if "expected" in case:
        measure = case["cube_query"]["measures"][0]
        assert len(rows) == 1, f"golden-number query must return one row, got {len(rows)}"
        got = to_number(rows[0][measure])
        want = float(case["expected"])
        dq.observe(observed=got, expected=want)
        assert close(got, want, abs_tol, rel_tol), (
            f"{case['id']}: Cube={got:,.2f} golden={want:,.2f} (source: {case['source']})")
        return

    bq = request.getfixturevalue("bq")
    ref = bq.query(case["bigquery_sql"])
    problems = compare_rowsets(rows, ref, case.get("keys") or {}, case["values"],
                               abs_tol, rel_tol, case.get("time_precision", "date"))
    dq.observe(observed={"mismatches": len(problems), "cube_rows": len(rows)},
               expected={"mismatches": 0, "bigquery_rows": len(ref)})
    shown = "\n  ".join(problems[:25])
    more = f"\n  ... and {len(problems) - 25} more" if len(problems) > 25 else ""
    assert not problems, f"{case['id']}: {len(problems)} mismatch(es)\n  {shown}{more}"
