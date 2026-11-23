"""Bug #4: the row-filter check must find the predicate on the right column with exactly the
allowed values. The old check passed if the value appeared anywhere in the SQL or params."""
from __future__ import annotations

import pytest

from semtest.sqlcheck import filter_values, row_filter_problem

pytestmark = pytest.mark.unit

SELECT = ("SELECT sum(`base_orders`.net_amount) `orders__net_revenue` "
          "FROM `proj`.`gold`.`fct_orders` AS `base_orders` ")


def test_bigquery_style_placeholder_on_the_right_column_passes() -> None:
    sql = SELECT + "WHERE (`base_orders`.state = ?) LIMIT 10000"
    assert row_filter_problem(sql, ["NSW"], "state", ["NSW"]) is None


def test_in_list_and_quoted_identifiers() -> None:
    sql = SELECT + 'WHERE ("base_orders"."state" IN (?, ?))'
    assert filter_values(sql, ["NSW", "ACT"], "state") == ["NSW", "ACT"]
    assert row_filter_problem(sql, ["NSW", "ACT"], "state", ["ACT", "NSW"]) is None


def test_placeholders_are_counted_in_order() -> None:
    sql = SELECT + "WHERE (`base_orders`.region = ?) AND (`base_orders`.state = ?)"
    assert filter_values(sql, ["NSW", "VIC"], "state") == ["VIC"]


def test_value_present_only_on_another_column_fails() -> None:
    """Old check: 'NSW' is in params, so it passed. The state column is not filtered at all."""
    sql = SELECT + "WHERE (`base_orders`.ship_state = ?)"
    assert "no `state" in row_filter_problem(sql, ["NSW"], "state", ["NSW"])


def test_short_value_matching_trivially_in_sql_text_fails() -> None:
    """Old check: '1' appears in the SQL text (`LIMIT 10000`), so it passed with no filter."""
    sql = SELECT + "LIMIT 10000"
    assert row_filter_problem(sql, [], "store_id", [1]) is not None


def test_filter_that_also_allows_other_values_fails() -> None:
    sql = SELECT + "WHERE (`base_orders`.state IN (?, ?))"
    problem = row_filter_problem(sql, ["NSW", "VIC"], "state", ["NSW"])
    assert problem and "also allows ['VIC']" in problem


def test_filter_missing_an_allowed_value_fails() -> None:
    sql = SELECT + "WHERE (`base_orders`.state = ?)"
    assert "does not include ['VIC']" in row_filter_problem(sql, ["NSW"], "state", ["NSW", "VIC"])


def test_inlined_literals_and_numbered_placeholders() -> None:
    assert filter_values("SELECT 1 FROM t WHERE t.state = 'NSW'", [], "state") == ["NSW"]
    assert filter_values("SELECT 1 FROM t WHERE t.state IN ($2, $1)", ["A", "B"],
                         "state") == ["B", "A"]


def test_question_marks_inside_string_literals_are_not_placeholders() -> None:
    sql = "SELECT 'why?' AS x FROM t WHERE t.state = ?"
    assert filter_values(sql, ["NSW"], "state") == ["NSW"]
