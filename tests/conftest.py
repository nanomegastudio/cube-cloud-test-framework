"""Shared fixtures: a Cube client, per-persona /meta, and an optional BigQuery runner.

Tiers (pytest markers):
  contract  needs Cube only; reads /meta and /v1/sql, never executes against BigQuery
  data      executes queries through Cube (so BigQuery is billed): row values, fan-out
  parity    compares Cube with golden numbers / BigQuery reference SQL
  rollup    pre-aggregation routing, materialisation, values and freshness
"""
from __future__ import annotations

import functools
from typing import Any

import pytest

from semtest import config
from semtest import meta as m
from semtest.cube import DEFAULT_ROW_LIMIT, ControlPlane, CubeClient
# Results recording (no-op unless suite.yml › results or SEMTEST_RESULTS turns it on).
from semtest.pytest_results import (  # noqa: F401  (pytest finds hooks and fixtures by name)
    dq, pytest_configure, pytest_runtest_makereport, pytest_sessionfinish,
    pytest_terminal_summary, set_owner)

config.load_dotenv()


@pytest.fixture(autouse=True)
def _owner_from_view_meta(request) -> None:
    """For a check parametrized by `view`, default its stored owner to the view's meta.owner.

    Only looks when a Cube client is already in use by the test, so unit and config-only tests
    never touch the network.
    """
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    view = params.get("view")
    uses_cube = "storefront_meta" in request.fixturenames or "meta_for" in request.fixturenames
    if not view or not uses_cube:
        return
    try:
        entry = m.get(request.getfixturevalue("storefront_meta"), view)
    except (pytest.skip.Exception, Exception):  # noqa: BLE001 - owner is best-effort
        return
    set_owner(request.node, ((entry or {}).get("meta") or {}).get("owner"))


@pytest.fixture(scope="session")
def suite_cfg() -> dict[str, Any]:
    return config.suite()


@pytest.fixture(scope="session")
def personas() -> dict[str, dict[str, Any]]:
    found = config.personas()
    if not found:
        pytest.fail("config/personas.yml defines no personas")
    return found


@pytest.fixture(scope="session")
def cube(suite_cfg) -> CubeClient:
    url = config.env("CUBE_API_URL")
    secret = config.env("CUBE_API_SECRET")
    if not url or not secret:
        pytest.skip("CUBE_API_URL and CUBE_API_SECRET are not set")
    return CubeClient(url, secret, timeout=int(config.env("CUBE_QUERY_TIMEOUT", "300")),
                      row_limit=int(suite_cfg.get("row_limit", DEFAULT_ROW_LIMIT)))


@pytest.fixture(scope="session")
def meta_for(cube: CubeClient, personas: dict[str, dict[str, Any]]):
    """meta_for(persona_name) -> that persona's /meta (cached for the session)."""

    @functools.lru_cache(maxsize=None)
    def _meta(name: str) -> dict[str, Any]:
        return cube.meta(personas[name]["claims"])

    return _meta


@pytest.fixture(scope="session")
def storefront_meta(meta_for, suite_cfg) -> dict[str, Any]:
    return meta_for(suite_cfg.get("storefront_persona", "ci_reader"))


@pytest.fixture(scope="session")
def default_claims(personas, suite_cfg) -> dict[str, Any]:
    return personas[suite_cfg.get("default_persona", "ci_reader")]["claims"]


@pytest.fixture(scope="session")
def control_plane() -> ControlPlane:
    host = config.env("CUBE_CLOUD_HOST")
    key = config.env("CUBE_CLOUD_API_KEY")
    deployment = config.env("CUBE_DEPLOYMENT_ID")
    if not (host and key and deployment):
        pytest.skip("CUBE_CLOUD_HOST / CUBE_CLOUD_API_KEY / CUBE_DEPLOYMENT_ID not set")
    return ControlPlane(host, key, deployment)


@pytest.fixture(scope="session")
def bq():
    project = config.env("BQ_PROJECT")
    if not project:
        pytest.skip("BQ_PROJECT not set (needed for parity and rollup value checks)")
    try:
        from semtest.bq import BigQueryRunner
        return BigQueryRunner(
            project=project,
            location=config.env("BQ_LOCATION", "australia-southeast1"),
            max_bytes_billed=int(config.env("BQ_MAX_BYTES_BILLED", str(5 * 1024**3))),
            labels={"app": "cube-semantic-tests",
                    "run": (config.env("CI_RUN_ID", "local") or "local").lower()[:63]},
        )
    except ImportError:
        pytest.skip("google-cloud-bigquery is not installed")


@pytest.fixture(scope="session")
def tol(suite_cfg) -> tuple[float, float]:
    t = suite_cfg.get("tolerance", {})
    return float(t.get("abs", 0.01)), float(t.get("rel", 1e-6))
