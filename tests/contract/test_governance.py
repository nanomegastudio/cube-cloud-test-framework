"""Contract tier: metadata quality, per public view.

Parametrised from suite.yml's expected_views (no network at collection time); each test reads
the live /meta, so members are always checked as published.
"""
from __future__ import annotations

import re

import pytest

from semtest import config
from semtest import meta as m

pytestmark = pytest.mark.contract

VIEWS = sorted(config.suite().get("expected_views", []))


def _view(storefront_meta, name):
    v = m.get(storefront_meta, name)
    if v is None:
        pytest.fail(f"{name} is not published (see test_storefront)")
    return v


@pytest.mark.parametrize("view", VIEWS)
def test_view_carries_required_meta(storefront_meta, suite_cfg, view) -> None:
    """Ownership lives in the model: owner, steward, data product (configurable)."""
    required = suite_cfg.get("governance", {}).get("required_view_meta", [])
    present = (_view(storefront_meta, view).get("meta") or {})
    missing = [k for k in required if not present.get(k)]
    assert not missing, f"{view}: meta is missing {missing}"


@pytest.mark.parametrize("view", VIEWS)
def test_view_and_members_are_documented(storefront_meta, suite_cfg, view) -> None:
    v = _view(storefront_meta, view)
    allow = set(suite_cfg.get("governance", {}).get("undocumented_allowlist", []))
    problems = [] if (v.get("description") or "").strip() else [f"{view} (the view itself)"]
    problems += [mem["name"] for mem in m.measures(v) + m.dimensions(v) + m.segments(v)
                 if not (mem.get("description") or "").strip() and mem["name"] not in allow]
    assert not problems, f"{view}: no description on {problems}"


@pytest.mark.parametrize("view", VIEWS)
def test_non_additive_measures_are_flagged(storefront_meta, view) -> None:
    """Ratios, averages and DISTINCT counts must declare meta.additive: false.

    Summing these across a breakdown produces a number that cannot exist — the classic BI and
    AI-agent error. countDistinct is included: distinct counts do not add across groups.
    """
    unflagged = [mem["name"] for mem in m.measures(_view(storefront_meta, view))
                 if m.agg_type(mem) in m.NON_ADDITIVE_AGG_TYPES
                 and (mem.get("meta") or {}).get("additive") is not False]
    assert not unflagged, f"{view}: non-additive measures without meta.additive=false: {unflagged}"


@pytest.mark.parametrize("view", VIEWS)
def test_pii_is_confined(storefront_meta, suite_cfg, view) -> None:
    """PII (by meta tag OR by name pattern) appears only in the allowed views."""
    gov = suite_cfg.get("governance", {})
    if view in set(gov.get("pii_allowed_views", [])):
        return
    patterns = [re.compile(p) for p in gov.get("pii_name_patterns", [])]
    v = _view(storefront_meta, view)
    leaked = [d["name"] for d in m.dimensions(v)
              if (d.get("meta") or {}).get("pii") is True
              or (d.get("meta") or {}).get("classification") == "pii"
              or any(p.search(m.short(d["name"])) for p in patterns)]
    assert not leaked, f"{view} exposes PII outside its allowed views: {leaked}"


@pytest.mark.parametrize("view", VIEWS)
def test_members_are_classified(storefront_meta, suite_cfg, view) -> None:
    if not suite_cfg.get("governance", {}).get("require_classification"):
        pytest.skip("require_classification is off")
    v = _view(storefront_meta, view)
    missing = [mem["name"] for mem in m.measures(v) + m.dimensions(v)
               if not (mem.get("meta") or {}).get("classification")]
    assert not missing, f"{view}: members without meta.classification: {missing}"


@pytest.mark.parametrize("view", VIEWS)
def test_fact_view_has_a_time_dimension(storefront_meta, suite_cfg, view) -> None:
    if view in set(suite_cfg.get("catalog_views", [])):
        pytest.skip(f"{view} is a catalog view")
    assert m.time_dimensions(_view(storefront_meta, view)), f"{view}: no time dimension"
