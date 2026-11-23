"""Data tier: grouping by a JOINED dimension must not inflate an additive total.

Pairs are generated from /meta: for each additive measure (sum/count, not flagged
meta.additive: false), take dimensions whose `aliasMember` comes from a DIFFERENT cube, i.e.
reached through a join. Grand total vs sum-of-groups; if the sum grows, the join is duplicating
fact rows (usually a missing or non-unique primary_key, or a wrong relationship).

A dimension on the MANY side of a join legitimately overlaps groups (one order, several
categories). Those pairs go in suite.yml › fanout.allowlist with a reason.
"""
from __future__ import annotations

import pytest

from semtest import cases, config
from semtest import meta as m

pytestmark = pytest.mark.data

VIEWS = sorted(config.suite().get("expected_views", []))


def _pairs(view_entry, max_pairs):
    pairs = []
    for meas in m.measures(view_entry):
        if not m.is_additive(meas):
            continue
        home = m.owning_cube(meas)
        for dim in m.dimensions(view_entry):
            if dim.get("type") == "time":
                continue
            if home and m.owning_cube(dim) and m.owning_cube(dim) != home:
                pairs.append((meas["name"], dim["name"]))
    return pairs[:max_pairs]


def _total(rows, measure):
    return sum(float(r[measure] or 0) for r in rows)


@pytest.mark.parametrize("view", VIEWS)
def test_joined_dimensions_do_not_inflate_totals(cube, storefront_meta, suite_cfg,
                                                 default_claims, tol, view, dq) -> None:
    v = m.get(storefront_meta, view)
    if v is None:
        pytest.fail(f"{view} is not published")
    fan = suite_cfg.get("fanout", {})
    allow = {(a["measure"], a["dimension"]) for a in (fan.get("allowlist") or [])}
    pairs = [p for p in _pairs(v, int(fan.get("max_pairs_per_view", 6))) if p not in allow]
    if not pairs:
        pytest.skip(f"{view}: no additive measure with a joined dimension")

    abs_tol, rel_tol = tol
    # Same bounded window as smoke, to cap BigQuery cost; both queries use the identical window.
    tds = m.time_dimensions(v)
    rng = cases.smoke_window(suite_cfg)
    window = [{"dimension": tds[0]["name"], "dateRange": rng}] if (tds and rng) else []
    problems = []
    for measure, dim in pairs:
        grand = _total(cube.rows_all({"measures": [measure], "timeDimensions": window},
                                     default_claims), measure)
        # rows_all raises TruncatedResult rather than returning a cut-off split, which would
        # otherwise show up here as a false "SHRANK".
        split = _total(cube.rows_all({"measures": [measure], "dimensions": [dim],
                                      "timeDimensions": window}, default_claims), measure)
        if abs(split - grand) > max(abs_tol, rel_tol * abs(grand)):
            direction = "INFLATED (fan-out)" if split > grand else "SHRANK (rows dropped by join)"
            problems.append(f"{measure} by {dim}: {grand:,.2f} -> {split:,.2f} {direction}")
    dq.observe(observed={"pairs_checked": len(pairs), "pairs_failing": len(problems)},
               expected={"pairs_failing": 0})
    assert not problems, f"{view}:\n  " + "\n  ".join(problems)
