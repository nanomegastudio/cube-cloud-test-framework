"""Data tier: every public measure and time dimension actually runs.

A measure whose SQL references a missing column compiles fine and only fails when queried. One
batched query per view; on failure, members are retried one by one to name the culprit.
Bounded by suite.yml › smoke_date_range to keep BigQuery cost down.
"""
from __future__ import annotations

import pytest

from semtest import config
from semtest import meta as m
from semtest.cube import CubeError

pytestmark = pytest.mark.data

VIEWS = sorted(config.suite().get("expected_views", []))


def _bounded(query, view_entry, date_range):
    tds = m.time_dimensions(view_entry)
    if date_range and tds:
        query["timeDimensions"] = [{"dimension": tds[0]["name"], "dateRange": date_range}]
    return query


@pytest.mark.parametrize("view", VIEWS)
def test_every_measure_answers(cube, storefront_meta, suite_cfg, default_claims, view) -> None:
    v = m.get(storefront_meta, view)
    names = [x["name"] for x in m.measures(v or {})]
    if not names:
        pytest.fail(f"{view}: no measures")
    rng = suite_cfg.get("smoke_date_range")
    try:
        cube.load(_bounded({"measures": names, "limit": 1}, v, rng), default_claims)
        return
    except CubeError:
        pass
    failing = []
    for name in names:
        try:
            cube.load(_bounded({"measures": [name], "limit": 1}, v, rng), default_claims)
        except CubeError as exc:
            failing.append(f"{name}: {str(exc).splitlines()[0][:200]}")
    assert not failing, f"{view}:\n  " + "\n  ".join(failing)


@pytest.mark.parametrize("view", VIEWS)
def test_every_time_dimension_slices_by_month(cube, storefront_meta, suite_cfg,
                                              default_claims, view) -> None:
    v = m.get(storefront_meta, view)
    tds = m.time_dimensions(v or {})
    if not tds:
        pytest.skip(f"{view}: no time dimension")
    measure = m.measures(v)[0]["name"]
    rng = suite_cfg.get("smoke_date_range")
    for td in tds:
        spec = {"dimension": td["name"], "granularity": "month"}
        if rng:
            spec["dateRange"] = rng
        cube.load({"measures": [measure], "timeDimensions": [spec], "limit": 3}, default_claims)
