"""Helpers for reading Cube's /v1/meta response.

Test cases (fan-out pairs, row-filter targets, governance checks) are DERIVED from /meta rather
than hand-listed, so a new view or member is covered the moment it is published.
"""
from __future__ import annotations

from typing import Any

# Measure aggregation types that are safe to add up across a breakdown.
ADDITIVE_AGG_TYPES = {"sum", "count"}
# Measure aggregation types whose values must NOT be summed across a breakdown.
NON_ADDITIVE_AGG_TYPES = {"number", "avg", "countdistinct", "countdistinctapprox",
                          "runningtotal", "min", "max"}


def entries(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """All cubes and views in a /meta response."""
    return meta.get("cubes", [])


def is_view(entry: dict[str, Any], private_prefix: str) -> bool:
    """True for a view. Uses /meta's `type` when present, else the private-cube naming rule."""
    kind = entry.get("type")
    if kind:
        return kind == "view"
    return not entry["name"].startswith(private_prefix)


def views(meta: dict[str, Any], private_prefix: str) -> list[dict[str, Any]]:
    return [e for e in entries(meta) if is_view(e, private_prefix)]


def get(meta: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((e for e in entries(meta) if e["name"] == name), None)


def measures(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("measures") or []


def dimensions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("dimensions") or []


def segments(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return entry.get("segments") or []


def short(member_name: str) -> str:
    """'orders.total_revenue' -> 'total_revenue'."""
    return member_name.split(".", 1)[-1]


def owning_cube(member: dict[str, Any]) -> str | None:
    """The private cube a view member really comes from, via `aliasMember` ('base_orders.x')."""
    alias = member.get("aliasMember")
    return alias.split(".", 1)[0] if alias else None


def agg_type(measure: dict[str, Any]) -> str:
    return (measure.get("aggType") or measure.get("type") or "").lower()


def is_additive(measure: dict[str, Any]) -> bool:
    """Additive = a sum/count not explicitly flagged `meta.additive: false`."""
    if (measure.get("meta") or {}).get("additive") is False:
        return False
    return agg_type(measure) in ADDITIVE_AGG_TYPES


def time_dimensions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in dimensions(entry) if d.get("type") == "time"]


def find_dimension(entry: dict[str, Any], short_name: str) -> dict[str, Any] | None:
    return next((d for d in dimensions(entry) if short(d["name"]) == short_name), None)
