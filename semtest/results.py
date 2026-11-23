"""Store every check's outcome, so results can be trended and alerted on, not just read in a log.

One row per test per run: what was checked, when, by which run, observed vs expected, status,
severity and owner. Sinks:

  bigquery  appends to a partitioned BigQuery table (created on first use if auto_create is on),
            with a load job, so there is no streaming-insert cost or streaming buffer.
  jsonl     appends one JSON object per line to a local file (local runs, Airflow XCom/GCS, tests).

Turned on in suite.yml › results, or with SEMTEST_RESULTS=bigquery|jsonl|off, which wins, so CI
or an Airflow task can switch it on without editing config. Credentials are never configured
here: the BigQuery sink uses Application Default Credentials, exactly like the reference runner.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
from typing import Any, Iterable, Protocol

from semtest import config

# Status of a check in one run. `warn` = a warn-severity check that failed (reported, not fatal).
STATUSES = ("pass", "fail", "warn", "error", "skip")
SEVERITIES = ("error", "warn")
MESSAGE_LIMIT = 4000

# (name, BigQuery type, mode, description). The DDL below and the auto-created table use this.
SCHEMA: list[tuple[str, str, str, str]] = [
    ("run_id", "STRING", "REQUIRED", "One pytest session: CI_RUN_ID, else a generated id"),
    ("run_started_at", "TIMESTAMP", "REQUIRED", "When the pytest session started (UTC)"),
    ("recorded_at", "TIMESTAMP", "REQUIRED", "When this check finished (UTC)"),
    ("check_id", "STRING", "REQUIRED", "Stable id: pytest node id, e.g. tests/data/test_parity.py::test_x[case]"),
    ("check_name", "STRING", "REQUIRED", "Test function, e.g. test_cube_matches_reference"),
    ("case_id", "STRING", "NULLABLE", "Parametrize id (case, view, persona)"),
    ("tier", "STRING", "NULLABLE", "Marker tier: unit, contract, data, parity, rollup"),
    ("status", "STRING", "REQUIRED", "pass | fail | warn | error | skip"),
    ("severity", "STRING", "REQUIRED", "error | warn"),
    ("observed", "STRING", "NULLABLE", "What Cube returned (JSON)"),
    ("expected", "STRING", "NULLABLE", "What it should have been (JSON)"),
    ("observed_value", "FLOAT64", "NULLABLE", "Observed, when it is a single number (for trending)"),
    ("expected_value", "FLOAT64", "NULLABLE", "Expected, when it is a single number"),
    ("message", "STRING", "NULLABLE", "Failure or skip reason, truncated"),
    ("owner", "STRING", "NULLABLE", "Case `owner`, else the view's meta.owner"),
    ("environment", "STRING", "NULLABLE", "SEMTEST_ENVIRONMENT, e.g. prod or the branch name"),
    ("duration_s", "FLOAT64", "NULLABLE", "Test duration in seconds"),
    ("git_sha", "STRING", "NULLABLE", "GITHUB_SHA / GIT_SHA if set"),
]


def ddl(table: str) -> str:
    """CREATE TABLE statement for `project.dataset.table` (same schema auto_create uses)."""
    cols = ",\n".join(
        f"  {name} {kind}{' NOT NULL' if mode == 'REQUIRED' else ''} OPTIONS(description={json.dumps(desc)})"
        for name, kind, mode, desc in SCHEMA)
    return (f"CREATE TABLE IF NOT EXISTS `{table}` (\n{cols}\n)\n"
            f"PARTITION BY DATE(run_started_at)\nCLUSTER BY check_name, status\n"
            f"OPTIONS(description=\"Cube semantic test results, one row per check per run\")")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, sort_keys=True)


@dataclasses.dataclass
class CheckResult:
    """One check's outcome in one run."""

    run_id: str
    run_started_at: dt.datetime
    check_id: str
    check_name: str
    status: str
    severity: str = "error"
    case_id: str | None = None
    tier: str | None = None
    observed: Any = None
    expected: Any = None
    message: str | None = None
    owner: str | None = None
    environment: str | None = None
    duration_s: float | None = None
    git_sha: str | None = None
    recorded_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status {self.status!r} not in {STATUSES}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity {self.severity!r} not in {SEVERITIES}")

    def to_row(self) -> dict[str, Any]:
        """A JSON-ready row matching SCHEMA."""
        msg = self.message
        if msg and len(msg) > MESSAGE_LIMIT:
            msg = msg[:MESSAGE_LIMIT] + " …[truncated]"
        return {
            "run_id": self.run_id,
            "run_started_at": self.run_started_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "check_id": self.check_id,
            "check_name": self.check_name,
            "case_id": self.case_id,
            "tier": self.tier,
            "status": self.status,
            "severity": self.severity,
            "observed": _json(self.observed),
            "expected": _json(self.expected),
            "observed_value": _number(self.observed),
            "expected_value": _number(self.expected),
            "message": msg,
            "owner": self.owner,
            "environment": self.environment,
            "duration_s": self.duration_s,
            "git_sha": self.git_sha,
        }


class Sink(Protocol):
    def write(self, rows: list[dict[str, Any]]) -> None: ...
    def describe(self) -> str: ...


class JsonlSink:
    """Appends rows to a JSON Lines file (created with its folder if missing)."""

    def __init__(self, path: str | pathlib.Path) -> None:
        path = pathlib.Path(path).expanduser()
        self.path = path if path.is_absolute() else config.ROOT / path

    def write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, default=str) + "\n")

    def describe(self) -> str:
        return str(self.path)


class BigQuerySink:
    """Appends rows to a BigQuery table with a load job.

    Args:
        table: Fully qualified `project.dataset.table`.
        location: Dataset location, used if the dataset or table has to be created.
        auto_create: Create the dataset and table (SCHEMA, partitioned, clustered) if missing.
            Off means the table must exist (create it from `python -m semtest.results --ddl`).
        client: A google.cloud.bigquery.Client, or a stand-in in tests.
    """

    def __init__(self, table: str, location: str | None = None, auto_create: bool = True,
                 client: Any = None, bigquery_module: Any = None) -> None:
        if table.count(".") != 2:
            raise config.ConfigError(f"results table {table!r} must be project.dataset.table")
        if bigquery_module is None:
            from google.cloud import bigquery as bigquery_module  # lazy: optional dependency
        self.bq = bigquery_module
        self.table = table
        self.location = location
        self.auto_create = auto_create
        self.client = client or bigquery_module.Client(project=table.split(".")[0])

    def _schema(self) -> list[Any]:
        return [self.bq.SchemaField(name, kind, mode=mode, description=desc)
                for name, kind, mode, desc in SCHEMA]

    def ensure_table(self) -> None:
        project, dataset, _ = self.table.split(".")
        ds = self.bq.Dataset(f"{project}.{dataset}")
        if self.location:
            ds.location = self.location
        self.client.create_dataset(ds, exists_ok=True)
        table = self.bq.Table(self.table, schema=self._schema())
        table.time_partitioning = self.bq.TimePartitioning(field="run_started_at")
        table.clustering_fields = ["check_name", "status"]
        self.client.create_table(table, exists_ok=True)

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.auto_create:
            self.ensure_table()
        job_config = self.bq.LoadJobConfig(
            schema=self._schema(),
            write_disposition="WRITE_APPEND",
            source_format="NEWLINE_DELIMITED_JSON",
        )
        job = self.client.load_table_from_json(rows, self.table, job_config=job_config,
                                               location=self.location)
        job.result()

    def describe(self) -> str:
        return f"bigquery:{self.table}"


def results_settings() -> dict[str, Any]:
    """suite.yml › results, with SEMTEST_RESULTS / SEMTEST_RESULTS_TABLE env overrides applied."""
    cfg = dict(config.suite().get("results") or {})
    override = config.env("SEMTEST_RESULTS")
    if override:
        override = override.lower()
        cfg["sink"] = None if override in ("off", "none", "false", "0") else override
    elif not cfg.get("enabled"):
        cfg["sink"] = None
    if config.env("SEMTEST_RESULTS_TABLE"):
        cfg.setdefault("bigquery", {})
        cfg["bigquery"] = {**cfg["bigquery"], "table": config.env("SEMTEST_RESULTS_TABLE")}
    return cfg


def build_sink(settings: dict[str, Any] | None = None) -> Sink | None:
    """The configured sink, or None when results are off."""
    s = results_settings() if settings is None else settings
    kind = s.get("sink")
    if not kind:
        return None
    if kind == "jsonl":
        return JsonlSink(s.get("jsonl_path") or "results/check_results.jsonl")
    if kind == "bigquery":
        b = s.get("bigquery") or {}
        table = b.get("table")
        if table and table.count(".") == 1:  # dataset.table: add the project
            project = b.get("project") or config.env("BQ_RESULTS_PROJECT") or config.env("BQ_PROJECT")
            table = f"{project}.{table}" if project else table
        if not table:
            raise config.ConfigError("results sink is bigquery but results.bigquery.table is not set")
        return BigQuerySink(table, location=b.get("location") or config.env("BQ_LOCATION"),
                            auto_create=bool(b.get("auto_create", True)))
    raise config.ConfigError(f"unknown results sink {kind!r}; use bigquery, jsonl or off")


def write_all(sink: Sink, results: Iterable[CheckResult]) -> int:
    rows = [r.to_row() for r in results]
    sink.write(rows)
    return len(rows)


if __name__ == "__main__":  # python -m semtest.results --ddl [project.dataset.table]
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "--ddl":
        print(ddl(sys.argv[2] if len(sys.argv) > 2 else "your-project.semantic_tests.check_results"))
    else:
        print("usage: python -m semtest.results --ddl [project.dataset.table]")
