"""pytest hooks that turn each test into a stored CheckResult (see semtest/results.py).

Loaded by tests/conftest.py. Does nothing unless a results sink is configured.

In a test:
    def test_x(dq, ...):
        dq.observe(observed=got, expected=want)     # stored as observed/expected (+ numeric copies)
        dq.owner = "finance-data"                   # else the case's `owner`, else view meta.owner

Severity:
    @pytest.mark.severity("warn") on a test, or `severity: warn` on a config case. A failing
    warn-level check is stored as status "warn", listed in the terminal summary, and does NOT
    fail the run. Everything else is "error" severity and fails as usual.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from semtest import config as semconfig
from semtest.results import CheckResult, build_sink, results_settings, write_all

TIERS = ("unit", "contract", "data", "parity", "rollup")

_STATE = pytest.StashKey["_RunState"]()
_ITEM = pytest.StashKey["_ItemState"]()


@dataclass
class _ItemState:
    observed: Any = None
    expected: Any = None
    owner: str | None = None
    status: str | None = None
    message: str | None = None
    duration: float = 0.0


@dataclass
class _RunState:
    sink: Any
    run_id: str
    started: dt.datetime
    environment: str | None
    git_sha: str | None
    results: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    written: str | None = None


class DQ:
    """What a test reports beyond pass/fail. Use via the `dq` fixture."""

    def __init__(self, state: _ItemState) -> None:
        self._state = state

    def observe(self, observed: Any = None, expected: Any = None) -> None:
        self._state.observed = observed
        self._state.expected = expected

    @property
    def owner(self) -> str | None:
        return self._state.owner

    @owner.setter
    def owner(self, value: str | None) -> None:
        self._state.owner = value


def _item_state(item: pytest.Item) -> _ItemState:
    if _ITEM not in item.stash:
        item.stash[_ITEM] = _ItemState()
    return item.stash[_ITEM]


@pytest.fixture
def dq(request) -> DQ:
    """Record observed/expected values and the owner for this check's stored result."""
    return DQ(_item_state(request.node))


def _params(item: pytest.Item) -> dict[str, Any]:
    callspec = getattr(item, "callspec", None)
    return dict(callspec.params) if callspec else {}


def severity_of(item: pytest.Item) -> str:
    marker = item.get_closest_marker("severity")
    if marker and marker.args:
        return str(marker.args[0])
    case = _params(item).get("case")
    if isinstance(case, dict) and case.get("severity"):
        return str(case["severity"])
    return "error"


def set_owner(item: pytest.Item, owner: str | None) -> None:
    """Default owner for a check (a test's own `dq.owner = ...` still wins)."""
    st = _item_state(item)
    if owner and not st.owner:
        st.owner = owner


def _owner_from_params(item: pytest.Item) -> str | None:
    case = _params(item).get("case")
    return case.get("owner") if isinstance(case, dict) else None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "severity(level): 'error' (default) or 'warn'; a failing "
                                       "warn check is recorded as warn and does not fail the run")
    try:
        sink = build_sink()
    except semconfig.ConfigError as exc:
        raise pytest.UsageError(f"results: {exc}") from exc
    except ImportError as exc:
        raise pytest.UsageError(f"results: the BigQuery sink needs google-cloud-bigquery ({exc})")
    config.stash[_STATE] = _RunState(
        sink=sink,
        run_id=semconfig.env("CI_RUN_ID") or f"local-{uuid.uuid4().hex[:12]}",
        started=dt.datetime.now(dt.timezone.utc),
        environment=semconfig.env("SEMTEST_ENVIRONMENT"),
        git_sha=semconfig.env("GITHUB_SHA") or semconfig.env("GIT_SHA"),
    )


def _message(rep: pytest.TestReport) -> str | None:
    if rep.skipped and isinstance(rep.longrepr, tuple):
        return str(rep.longrepr[2])
    if not rep.longrepr:
        return None
    return rep.longreprtext or None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    rep: pytest.TestReport = outcome.get_result()
    run = item.config.stash.get(_STATE, None)
    st = _item_state(item)
    st.duration += rep.duration or 0.0

    # A failing warn-level check is reported, not fatal.
    if rep.when == "call" and rep.failed and severity_of(item) == "warn":
        st.status, st.message = "warn", _message(rep)
        if run is not None:
            first = (st.message or "").strip().splitlines()[-1:] or [""]
            run.warnings.append(f"{item.nodeid}: {first[0][:200]}")
        rep.outcome = "passed"
        rep.longrepr = None
    elif rep.when == "setup":
        if rep.skipped:
            st.status, st.message = "skip", _message(rep)
        elif rep.failed:
            st.status, st.message = "error", _message(rep)
    elif rep.when == "call" and st.status is None:
        if rep.skipped:
            st.status, st.message = "skip", _message(rep)
        else:
            st.status = "fail" if rep.failed else "pass"
            st.message = _message(rep) if rep.failed else None
    elif rep.when == "teardown":
        if rep.failed and st.status in (None, "pass"):
            st.status, st.message = "error", _message(rep)
        if run is not None and run.sink is not None:
            run.results.append(CheckResult(
                run_id=run.run_id,
                run_started_at=run.started,
                check_id=item.nodeid,
                check_name=getattr(item, "originalname", None) or item.name,
                case_id=getattr(getattr(item, "callspec", None), "id", None),
                tier=next((t for t in TIERS if item.get_closest_marker(t)), None),
                status=st.status or "error",
                severity=severity_of(item),
                observed=st.observed,
                expected=st.expected,
                message=st.message,
                owner=st.owner or _owner_from_params(item),
                environment=run.environment,
                duration_s=round(st.duration, 3),
                git_sha=run.git_sha,
            ))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    run = session.config.stash.get(_STATE, None)
    if run is None or run.sink is None or not run.results:
        return
    try:
        n = write_all(run.sink, run.results)
        run.written = f"{n} check results written to {run.sink.describe()} (run_id={run.run_id})"
    except Exception as exc:  # noqa: BLE001 - never lose the test verdict over the sink
        run.written = f"FAILED to write check results to {run.sink.describe()}: {exc}"
        if results_settings().get("fail_on_write_error"):
            session.exitstatus = pytest.ExitCode.INTERNAL_ERROR


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    run = config.stash.get(_STATE, None)
    if run is None:
        return
    if run.warnings:
        terminalreporter.section("warn-level checks that failed")
        for line in run.warnings:
            terminalreporter.line(line)
    if run.written:
        terminalreporter.line(f"results: {run.written}")

