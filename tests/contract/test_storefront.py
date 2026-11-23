"""Contract tier: the public storefront is exactly what was reviewed.

Views are the only public interface. Every consumer (Looker, apps, AI agents) sees the same
closed set, and a private cube never leaks.
"""
from __future__ import annotations

import re

import pytest

from semtest import meta as m

pytestmark = pytest.mark.contract

SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def test_storefront_is_exactly_the_expected_views(storefront_meta, suite_cfg) -> None:
    expected = set(suite_cfg.get("expected_views", []))
    actual = {v["name"] for v in m.views(storefront_meta, suite_cfg.get("private_prefix", "base_"))}
    assert actual == expected, (
        f"storefront drift — published but not in suite.yml: {sorted(actual - expected)}; "
        f"in suite.yml but not published: {sorted(expected - actual)}")


def test_no_private_cube_is_exposed(storefront_meta, suite_cfg) -> None:
    prefix = suite_cfg.get("private_prefix", "base_")
    leaked = [e["name"] for e in m.entries(storefront_meta) if not m.is_view(e, prefix)]
    assert not leaked, f"cubes visible through /meta (set public: false): {leaked}"


def test_member_names_are_snake_case(storefront_meta, suite_cfg) -> None:
    prefix = suite_cfg.get("private_prefix", "base_")
    bad = [mem["name"] for v in m.views(storefront_meta, prefix)
           for mem in m.measures(v) + m.dimensions(v) + m.segments(v)
           if not SNAKE.match(m.short(mem["name"]))]
    assert not bad, f"non snake_case members: {bad}"
