"""Data tier: a restricted persona gets back ONLY its allowed values.

The contract tier proves the predicate is in the SQL; this proves the rows that come back are
right, which also catches a filter on the wrong column or a join that reintroduces rows.
"""
from __future__ import annotations

import pytest

from semtest import config
from semtest import meta as m

pytestmark = pytest.mark.data

VIEWS = sorted(config.suite().get("expected_views", []))
RESTRICTED = sorted(n for n, p in config.personas().items() if p.get("row_values"))


@pytest.mark.parametrize("persona", RESTRICTED)
@pytest.mark.parametrize("view", VIEWS)
def test_only_allowed_values_come_back(cube, meta_for, suite_cfg, personas, persona, view) -> None:
    meta = meta_for(persona)
    v = m.get(meta, view)
    if v is None:
        pytest.skip(f"{persona} cannot see {view}")
    measure = m.measures(v)[0]["name"]
    checked = False
    for rule in suite_cfg.get("row_filters", []):
        if view in set(rule.get("exempt_views", [])):
            continue
        dim = m.find_dimension(v, rule["dimension"])
        allowed = personas[persona]["row_values"].get(rule["dimension"])
        if dim is None or allowed is None:
            continue
        # rows_all, not a fixed limit: a cut-off list could hide a leaked value (a false pass).
        rows = cube.rows_all({"measures": [measure], "dimensions": [dim["name"]]},
                             personas[persona]["claims"])
        returned = {r[dim["name"]] for r in rows}
        leaked = returned - {str(a) for a in allowed}
        assert not leaked, f"{persona} on {view} received {dim['name']} values {sorted(leaked)}"
        checked = True
    if not checked:
        pytest.skip(f"{view} exposes no row-filter dimension")
