"""The results table: rows, settings, the BigQuery sink (against a fake) and the pytest hooks."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap

import pytest

from semtest import config, results
from semtest.results import BigQuerySink, CheckResult, JsonlSink, build_sink, ddl

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 11, 23, 3, 0, tzinfo=dt.timezone.utc)


def _result(**kw) -> CheckResult:
    base = dict(run_id="r1", run_started_at=T0, check_id="tests/x.py::test_a[c1]",
                check_name="test_a", status="pass")
    return CheckResult(**{**base, **kw})


# ------------------------------------------------------------------ rows and DDL

def test_row_has_every_schema_column_and_numeric_copies() -> None:
    row = _result(observed=1234.5, expected="1234.50", owner="finance").to_row()
    assert set(row) == {name for name, *_ in results.SCHEMA}
    assert row["observed_value"] == 1234.5 and row["expected_value"] == 1234.5
    assert row["observed"] == "1234.5" and row["owner"] == "finance"


def test_structured_values_are_json_and_have_no_numeric_copy() -> None:
    row = _result(observed={"mismatches": 2}, expected={"mismatches": 0}).to_row()
    assert json.loads(row["observed"]) == {"mismatches": 2}
    assert row["observed_value"] is None


def test_long_messages_are_truncated() -> None:
    row = _result(status="fail", message="x" * 10000).to_row()
    assert len(row["message"]) < 4100 and row["message"].endswith("[truncated]")


def test_bad_status_or_severity_is_rejected() -> None:
    with pytest.raises(ValueError):
        _result(status="passed")
    with pytest.raises(ValueError):
        _result(severity="info")


def test_ddl_is_partitioned_and_lists_every_column() -> None:
    sql = ddl("p.d.t")
    assert "CREATE TABLE IF NOT EXISTS `p.d.t`" in sql
    assert "PARTITION BY DATE(run_started_at)" in sql
    for name, *_ in results.SCHEMA:
        assert f"  {name} " in sql


# ------------------------------------------------------------------ settings

@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    for var in ("SEMTEST_RESULTS", "SEMTEST_RESULTS_TABLE", "BQ_RESULTS_PROJECT", "BQ_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path / "no.env")
    monkeypatch.setenv("SEMTEST_CONFIG_DIR", str(tmp_path))

    def write(suite_yaml: str):
        (tmp_path / "suite.yml").write_text(textwrap.dedent(suite_yaml))
    write("expected_views: []\n")
    return write


def test_results_are_off_by_default(cfg_dir) -> None:
    assert build_sink() is None


def test_enabled_in_suite_yml(cfg_dir, tmp_path) -> None:
    cfg_dir(f"""
        results:
          enabled: true
          sink: jsonl
          jsonl_path: {tmp_path / 'out.jsonl'}
    """)
    sink = build_sink()
    assert isinstance(sink, JsonlSink) and sink.path == tmp_path / "out.jsonl"


def test_env_var_turns_it_on_or_off(cfg_dir, monkeypatch) -> None:
    monkeypatch.setenv("SEMTEST_RESULTS", "jsonl")
    assert isinstance(build_sink(), JsonlSink)
    cfg_dir("results: {enabled: true, sink: jsonl}\n")
    monkeypatch.setenv("SEMTEST_RESULTS", "off")
    assert build_sink() is None


def test_dataset_dot_table_takes_the_project_from_env(cfg_dir, monkeypatch) -> None:
    cfg_dir("results: {enabled: true, sink: bigquery, bigquery: {table: dq.check_results}}\n")
    monkeypatch.setenv("BQ_PROJECT", "acme-analytics")
    monkeypatch.setattr(results, "BigQuerySink",
                        lambda table, **kw: ("sink", table, kw["auto_create"]))
    assert build_sink() == ("sink", "acme-analytics.dq.check_results", True)


def test_bigquery_without_a_table_is_a_config_error(cfg_dir) -> None:
    cfg_dir("results: {enabled: true, sink: bigquery}\n")
    with pytest.raises(config.ConfigError, match="table"):
        build_sink()


# ------------------------------------------------------------------ BigQuery sink (fake client)

class _FakeBQ:
    """Just enough of google.cloud.bigquery for the sink."""

    class SchemaField:
        def __init__(self, name, kind, mode, description):
            self.name, self.kind, self.mode = name, kind, mode

    class Dataset:
        def __init__(self, ref):
            self.ref, self.location = ref, None

    class Table:
        def __init__(self, ref, schema):
            self.ref, self.schema = ref, schema
            self.time_partitioning = self.clustering_fields = None

    class TimePartitioning:
        def __init__(self, field):
            self.field = field

    class LoadJobConfig:
        def __init__(self, **kw):
            self.kw = kw


class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_dataset(self, ds, exists_ok):
        self.calls.append(("dataset", ds.ref, ds.location, exists_ok))

    def create_table(self, table, exists_ok):
        self.calls.append(("table", table.ref, table.time_partitioning.field,
                           table.clustering_fields, len(table.schema), exists_ok))

    def load_table_from_json(self, rows, table, job_config, location):
        self.calls.append(("load", table, list(rows), job_config.kw["write_disposition"],
                           location))
        return type("Job", (), {"result": lambda self: None})()


def test_bigquery_sink_creates_the_table_then_appends() -> None:
    client = _FakeClient()
    sink = BigQuerySink("p.dq.check_results", location="australia-southeast1",
                        client=client, bigquery_module=_FakeBQ)
    rows = [_result().to_row(), _result(status="fail").to_row()]
    sink.write(rows)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["dataset", "table", "load"]
    assert client.calls[0] == ("dataset", "p.dq", "australia-southeast1", True)
    assert client.calls[1][2:] == ("run_started_at", ["check_name", "status"],
                                   len(results.SCHEMA), True)
    load = client.calls[2]
    assert load[1] == "p.dq.check_results" and load[2] == rows and load[3] == "WRITE_APPEND"


def test_bigquery_sink_without_auto_create_only_loads() -> None:
    client = _FakeClient()
    BigQuerySink("p.dq.t", auto_create=False, client=client,
                 bigquery_module=_FakeBQ).write([_result().to_row()])
    assert [c[0] for c in client.calls] == ["load"]


def test_bigquery_sink_writes_nothing_for_no_rows() -> None:
    client = _FakeClient()
    BigQuerySink("p.dq.t", client=client, bigquery_module=_FakeBQ).write([])
    assert client.calls == []


def test_table_must_be_fully_qualified() -> None:
    with pytest.raises(config.ConfigError):
        BigQuerySink("dq.t", client=_FakeClient(), bigquery_module=_FakeBQ)


# ------------------------------------------------------------------ the pytest hooks, end to end

INNER_TESTS = '''
import pytest

CASES = [{"id": "rev", "owner": "finance-data"}, {"id": "soft", "owner": "ops", "severity": "warn"}]

def test_passes(dq):
    dq.observe(observed=100.0, expected=100.0)

def test_fails(dq):
    dq.owner = "sales-data"
    dq.observe(observed=99.0, expected=100.0)
    assert 99.0 == 100.0, "revenue is off by 1"

def test_skipped():
    pytest.skip("no BigQuery here")

@pytest.mark.severity("warn")
def test_warn_marker():
    assert False, "volume dipped 12%"

@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case, dq):
    dq.observe(observed=1, expected=2)
    assert case["id"] == "rev"

@pytest.fixture
def broken():
    raise RuntimeError("fixture blew up")

def test_setup_error(broken):
    pass
'''

INNER_CONFTEST = '''
from semtest.pytest_results import (  # noqa: F401
    dq, pytest_configure, pytest_runtest_makereport, pytest_sessionfinish,
    pytest_terminal_summary)
'''


def _run_inner(tmp_path, tests: str):
    root = tmp_path / "inner"
    (root / "cfg").mkdir(parents=True)
    out = tmp_path / "results.jsonl"
    (root / "cfg" / "suite.yml").write_text(
        f"results:\n  enabled: true\n  sink: jsonl\n  jsonl_path: {json.dumps(str(out))}\n")
    (root / "conftest.py").write_text(INNER_CONFTEST)
    (root / "test_inner.py").write_text(tests)
    (root / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, "PYTHONPATH": str(config.ROOT), "SEMTEST_CONFIG_DIR": str(root / "cfg"),
           "SEMTEST_RESULTS": "jsonl", "CI_RUN_ID": "unit-run-1",
           "SEMTEST_ENVIRONMENT": "unit"}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                           str(root)], cwd=root, env=env, capture_output=True, text=True,
                          timeout=120)
    rows = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
    return proc, {r["check_id"].split("::", 1)[1]: r for r in rows}


def test_every_check_is_recorded_with_the_right_status(tmp_path) -> None:
    proc, rows = _run_inner(tmp_path, INNER_TESTS)
    assert proc.returncode == 1, proc.stdout + proc.stderr     # test_fails and the setup error
    assert {k: v["status"] for k, v in rows.items()} == {
        "test_passes": "pass",
        "test_fails": "fail",
        "test_skipped": "skip",
        "test_warn_marker": "warn",
        "test_case[rev]": "pass",
        "test_case[soft]": "warn",
        "test_setup_error": "error",
    }
    assert {r["run_id"] for r in rows.values()} == {"unit-run-1"}
    assert all(r["environment"] == "unit" for r in rows.values())
    assert "warn-level checks that failed" in proc.stdout
    assert "7 check results written" in proc.stdout


def test_observed_expected_owner_and_severity_are_stored(tmp_path) -> None:
    _, rows = _run_inner(tmp_path, INNER_TESTS)
    fails = rows["test_fails"]
    assert (fails["observed_value"], fails["expected_value"]) == (99.0, 100.0)
    assert fails["owner"] == "sales-data" and "revenue is off by 1" in fails["message"]
    assert rows["test_case[rev]"]["owner"] == "finance-data"
    assert rows["test_case[soft]"]["severity"] == "warn"
    assert rows["test_warn_marker"]["severity"] == "warn"
    assert "volume dipped 12%" in rows["test_warn_marker"]["message"]
    assert "no BigQuery here" in rows["test_skipped"]["message"]
    assert "fixture blew up" in rows["test_setup_error"]["message"]


def test_warn_failures_alone_do_not_fail_the_run(tmp_path) -> None:
    proc, rows = _run_inner(tmp_path, textwrap.dedent('''
        import pytest

        @pytest.mark.severity("warn")
        def test_soft():
            assert False, "null share 3% (threshold 2%)"
    '''))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rows["test_soft"]["status"] == "warn"
