"""Bug #3: a result cut off at Cube's row limit must fail as truncated, not as a data mismatch."""
from __future__ import annotations

import pytest

from semtest.cube import CubeError, TruncatedResult
from tests.unit.fakes import fake_cube, fake_dq

pytestmark = pytest.mark.unit


def _serving(total_rows: int):
    """Cube with `total_rows` groups that honours the query's limit, like the real one."""
    def handler(query):
        n = min(total_rows, query.get("limit", 10000))
        return {"data": [{"v.state": f"S{i}", "v.amount": "1"} for i in range(n)]}
    return handler


def test_full_result_is_returned_and_limit_is_explicit() -> None:
    cube = fake_cube(_serving(99), row_limit=100)
    rows = cube.rows_all({"measures": ["v.amount"], "dimensions": ["v.state"]}, {})
    assert len(rows) == 99
    assert cube.session.queries[0]["limit"] == 100  # never left to Cube's silent default


def test_result_at_the_limit_raises_truncated() -> None:
    cube = fake_cube(_serving(250), row_limit=100)
    with pytest.raises(TruncatedResult, match="row limit"):
        cube.rows_all({"measures": ["v.amount"], "dimensions": ["v.state"]}, {})


def test_truncated_is_still_a_test_failure() -> None:
    assert issubclass(TruncatedResult, CubeError)
    assert issubclass(TruncatedResult, AssertionError)


def test_an_explicit_limit_is_a_deliberate_top_n() -> None:
    cube = fake_cube(_serving(250), row_limit=100)
    rows = cube.rows_all({"measures": ["v.amount"], "dimensions": ["v.state"], "limit": 5}, {})
    assert len(rows) == 5


def test_fanout_reports_truncation_not_shrank() -> None:
    """The old fan-out test summed a cut-off split and reported 'SHRANK (rows dropped by join)'."""
    from tests.data.test_fanout import test_joined_dimensions_do_not_inflate_totals as fanout

    def handler(query):
        if query.get("dimensions"):                       # split: 250 groups of 1
            n = min(250, query["limit"])
            return {"data": [{"v.amount": "1", "v.state": f"S{i}"} for i in range(n)]}
        return {"data": [{"v.amount": "250"}]}            # grand total
    cube = fake_cube(handler, row_limit=100)
    meta = {"cubes": [{
        "name": "v", "type": "view",
        "measures": [{"name": "v.amount", "aggType": "sum", "aliasMember": "base_orders.amount"}],
        "dimensions": [{"name": "v.state", "type": "string", "aliasMember": "base_stores.state"}],
    }]}
    with pytest.raises(TruncatedResult):
        fanout(cube, meta, {"fanout": {}}, {}, (0.01, 1e-6), "v", fake_dq())
