"""Run reference SQL directly against BigQuery, bypassing Cube.

Every job carries a bytes-billed cap and labels, so the suite cannot run up an unbounded bill and
its cost is attributable in billing exports.
"""
from __future__ import annotations

from typing import Any


class BigQueryRunner:
    """Thin wrapper over google-cloud-bigquery.

    Args:
        project: GCP project that runs (and pays for) the query jobs.
        location: Dataset location, e.g. "australia-southeast1".
        max_bytes_billed: Hard cap per job; BigQuery fails the job rather than exceed it.
        labels: Job labels for cost attribution.
    """

    def __init__(self, project: str, location: str, max_bytes_billed: int,
                 labels: dict[str, str] | None = None) -> None:
        from google.cloud import bigquery  # imported lazily: only the data tiers need it

        self._bigquery = bigquery
        self.client = bigquery.Client(project=project)
        self.location = location
        self.max_bytes_billed = max_bytes_billed
        self.labels = labels or {}

    def query(self, sql: str) -> list[dict[str, Any]]:
        """Run `sql` and return one dict per row."""
        job_config = self._bigquery.QueryJobConfig(
            maximum_bytes_billed=self.max_bytes_billed,
            labels=self.labels,
            use_query_cache=True,
        )
        job = self.client.query(sql, job_config=job_config, location=self.location)
        return [dict(row.items()) for row in job.result()]

    def scalar(self, sql: str) -> Any:
        """Run `sql` returning one row with one column and return that value."""
        rows = self.query(sql)
        assert rows, f"reference SQL returned no rows:\n{sql}"
        return next(iter(rows[0].values()))
