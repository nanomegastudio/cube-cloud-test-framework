"""Contract tier: access control fails closed, and every persona gets exactly its views and rows.

No query here is executed. Visibility is read from each persona's /meta; the row filter is read
from the SQL Cube GENERATES (/v1/sql), so this tier costs nothing in BigQuery.

Row-filter targets are discovered from /meta: any public view exposing a row-filter dimension
(suite.yml › row_filters) is checked, so a new view cannot escape the filter by being forgotten
in a hand-maintained list.
"""
from __future__ import annotations

import pytest

from semtest import config
from semtest import meta as m
from semtest.sqlcheck import row_filter_problem

pytestmark = pytest.mark.contract

VIEWS = sorted(config.suite().get("expected_views", []))
PERSONAS = config.personas()
RESTRICTED = sorted(n for n, p in PERSONAS.items() if p.get("row_values"))


def _visible(meta_for, suite_cfg, persona) -> set[str]:
    prefix = suite_cfg.get("private_prefix", "base_")
    return {v["name"] for v in m.views(meta_for(persona), prefix)}


# ------------------------------------------------------------------ authentication fails closed

def _assert_rejected_or_empty(resp, what: str, suite_cfg) -> None:
    """Rejected (401/403), or accepted with an empty storefront. Anything else is a leak."""
    if resp.status_code in (401, 403):
        return
    assert resp.status_code == 200, f"{what}: unexpected HTTP {resp.status_code}"
    views = m.views(resp.json(), suite_cfg.get("private_prefix", "base_"))
    assert not views, f"{what} was accepted and can see {[v['name'] for v in views]}"


def test_no_token_is_rejected(cube, suite_cfg) -> None:
    _assert_rejected_or_empty(cube.raw_get("meta"), "a request with no token", suite_cfg)


def test_bad_signature_is_rejected(cube, personas, suite_cfg) -> None:
    claims = personas[suite_cfg.get("storefront_persona", "ci_reader")]["claims"]
    forged = cube.mint_token(claims, secret="not-the-real-secret")
    _assert_rejected_or_empty(cube.raw_get("meta", {"Authorization": forged}),
                              "a token with a forged signature", suite_cfg)


def test_expired_token_is_rejected(cube, personas, suite_cfg) -> None:
    claims = personas[suite_cfg.get("storefront_persona", "ci_reader")]["claims"]
    stale = cube.mint_token(claims, expired=True)
    _assert_rejected_or_empty(cube.raw_get("meta", {"Authorization": stale}),
                              "an expired token", suite_cfg)


def test_token_with_no_access_claims_sees_nothing(cube, suite_cfg) -> None:
    bare = cube.mint_token({"sub": "test-no-claims"})
    _assert_rejected_or_empty(cube.raw_get("meta", {"Authorization": bare}),
                              "a valid token with no access claims", suite_cfg)


# ------------------------------------------------------------------ visibility per persona

@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_persona_sees_what_it_should(meta_for, suite_cfg, personas, persona) -> None:
    visible = _visible(meta_for, suite_cfg, persona)
    must = set(personas[persona].get("can_see", []))
    must_not = set(personas[persona].get("cannot_see", []))
    assert must <= visible, f"{persona} cannot see {sorted(must - visible)}"
    assert not (must_not & visible), f"{persona} can see {sorted(must_not & visible)}"


def test_storefront_persona_is_a_superset(meta_for, suite_cfg, personas) -> None:
    full = _visible(meta_for, suite_cfg, suite_cfg.get("storefront_persona", "ci_reader"))
    for persona in personas:
        extra = _visible(meta_for, suite_cfg, persona) - full
        assert not extra, f"{persona} sees views the storefront persona does not: {extra}"


# ------------------------------------------------------------------ row filter in generated SQL

def _filter_targets(meta, view, suite_cfg):
    """(rule, dimension member) for each row-filter rule this view is subject to."""
    v = m.get(meta, view)
    if v is None:
        return []
    out = []
    for rule in suite_cfg.get("row_filters", []):
        if view in set(rule.get("exempt_views", [])):
            continue
        dim = m.find_dimension(v, rule["dimension"])
        if dim is not None:
            out.append((rule, dim))
    return out


def _assert_filtered(sql_block, rule, allowed, label) -> None:
    """The generated SQL restricts the rule's column to exactly `allowed`.

    Checks the predicate on the right column (suite.yml › row_filters › sql_column, default the
    dimension's short name) and the values it binds, not just that the values appear somewhere.
    """
    text, params = sql_block["sql"][0], sql_block["sql"][1]
    column = rule.get("sql_column") or rule["dimension"]
    problem = row_filter_problem(text, params, column, allowed)
    assert problem is None, f"{label}: {problem}.\nparams={params}\nsql={text[:600]}"


@pytest.mark.parametrize("persona", RESTRICTED)
@pytest.mark.parametrize("view", VIEWS)
def test_row_filter_is_injected(cube, meta_for, suite_cfg, personas, persona, view) -> None:
    """Every view a restricted persona can see, and that exposes a filter dimension, is filtered."""
    meta = meta_for(persona)
    if m.get(meta, view) is None:
        pytest.skip(f"{persona} cannot see {view}")
    targets = _filter_targets(meta, view, suite_cfg)
    if not targets:
        pytest.skip(f"{view} exposes no row-filter dimension")
    measure = m.measures(m.get(meta, view))[0]["name"]
    for rule, _dim in targets:
        allowed = personas[persona]["row_values"].get(rule["dimension"])
        if allowed is None:
            continue
        block = cube.sql({"measures": [measure]}, personas[persona]["claims"])
        _assert_filtered(block, rule, allowed, f"{persona} on {view} (no {rule['dimension']} selected)")


@pytest.mark.parametrize("persona", RESTRICTED)
@pytest.mark.parametrize("view", VIEWS)
def test_segments_cannot_bypass_the_row_filter(cube, meta_for, suite_cfg, personas,
                                               persona, view) -> None:
    """A segment is a filter the caller chooses; the row filter must still AND onto it."""
    meta = meta_for(persona)
    v = m.get(meta, view)
    if v is None or not m.segments(v):
        pytest.skip(f"{view}: not visible to {persona}, or no segments")
    targets = _filter_targets(meta, view, suite_cfg)
    if not targets:
        pytest.skip(f"{view} exposes no row-filter dimension")
    measure = m.measures(v)[0]["name"]
    for seg in m.segments(v):
        for rule, _ in targets:
            allowed = personas[persona]["row_values"].get(rule["dimension"])
            if allowed is None:
                continue
            block = cube.sql({"measures": [measure], "segments": [seg["name"]]},
                             personas[persona]["claims"])
            _assert_filtered(block, rule, allowed, f"{persona} on {view} with segment {seg['name']}")
