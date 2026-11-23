"""Example Airflow DAG: dbt -> pre-prod gate -> publish -> prod check (-> rollback).

A sketch to adapt, not a drop-in: operator choices, connection ids, paths and the dbt command
depend on your Airflow setup. It runs the suite directly with pytest; if you prefer the GitHub
Actions jobs, replace the two pytest tasks with a dispatch plus a wait on the workflow run.

Nothing here holds credentials. The workers use their own Google identity (BigQuery Job User and
Data Editor on gold, gold_prev, gold_staging and ops), and the Cube secrets come from Airflow
connections or a secrets backend into the environment.
"""
from __future__ import annotations

import pendulum
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import ShortCircuitOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
from airflow.utils.trigger_rule import TriggerRule

SUITE = "/opt/airflow/repos/cube-model/semantic_tests"   # where this repo is checked out
PROJECT = "your-project"
GOLD_TABLES = ["fct_orders"]                             # every table the Cube model reads

COMMON_ENV = {
    "BQ_PROJECT": PROJECT,
    "BQ_LOCATION": "australia-southeast1",
    "SEMTEST_VAR_PROJECT": PROJECT,
    "CI_RUN_ID": "airflow-{{ run_id }}",
    "SEMTEST_RESULTS": "bigquery",
    "SEMTEST_RESULTS_TABLE": f"{PROJECT}.semantic_tests.check_results",
}


def _clone_sql(target: str, source: str) -> str:
    return "\n".join(
        f"CREATE OR REPLACE TABLE `{PROJECT}.{target}.{t}` CLONE `{PROJECT}.{source}.{t}`;"
        for t in GOLD_TABLES)


def _log_sql(table: str, action: str) -> str:
    return (f"INSERT INTO `{PROJECT}.ops.{table}` "
            f"VALUES (CURRENT_TIMESTAMP(), '{{{{ run_id }}}}', '{action}');")


def _bq(task_id: str, sql: str, **kw) -> BigQueryInsertJobOperator:
    return BigQueryInsertJobOperator(
        task_id=task_id,
        configuration={"query": {"query": sql, "useLegacySql": False}},
        location="australia-southeast1",
        **kw,
    )


with DAG(
    dag_id="gold_publish_with_cube_gate",
    schedule="0 2 * * *",
    start_date=pendulum.datetime(2026, 11, 1, tz="Australia/Sydney"),
    catchup=False,
    max_active_runs=1,
) as dag:
    dbt_build_staging = BashOperator(
        task_id="dbt_build_staging",
        bash_command="cd /opt/airflow/repos/dbt && dbt build --target staging",  # writes gold_staging
    )

    # Tells the pre-prod Cube (refresh_key on staging_build_log) to rebuild its rollups.
    mark_staging_built = _bq("mark_staging_built", _log_sql("staging_build_log", "built"))

    preprod_gate = BashOperator(
        task_id="preprod_gate",
        bash_command=(
            f"cd {SUITE} && python scripts/wait_for_rollups.py --timeout 1800 && "
            'pytest -m "contract or data or parity or rollup"'
        ),
        env={**COMMON_ENV,
             "CUBE_API_URL": "{{ var.value.cube_preprod_api_url }}",
             "CUBE_API_SECRET": "{{ conn.cube_preprod.password }}",
             "SEMTEST_VAR_GOLD_DATASET": "gold_staging",
             "SEMTEST_ENVIRONMENT": "preprod"},
        append_env=True,
    )

    # Only runs if the gate passed (default trigger rule: all upstream succeeded).
    # No transaction: BigQuery refuses DDL (CLONE) inside one. The log row goes last, so prod
    # rollups (refresh_key on publish_log) rebuild only after every table has switched.
    publish = _bq("publish", _clone_sql("gold_prev", "gold") + "\n"
                  + _clone_sql("gold", "gold_staging") + "\n"
                  + _log_sql("publish_log", "publish"))

    prod_check = BashOperator(
        task_id="prod_check",
        bash_command=(
            f"cd {SUITE} && python scripts/wait_for_rollups.py --timeout 1800 && "
            'pytest -m rollup -k "routed or materialised or fresh"'
        ),
        env={**COMMON_ENV,
             "CUBE_API_URL": "{{ var.value.cube_prod_api_url }}",
             "CUBE_API_SECRET": "{{ conn.cube_prod.password }}",
             "SEMTEST_ENVIRONMENT": "prod"},
        append_env=True,
    )

    # Roll back ONLY when this run published and the prod check then failed. A publish that fails
    # part way leaves the run red for a person to look at: gold_prev may be partly refreshed then. A plain
    # trigger_rule=one_failed would also fire when the gate failed (nothing was published),
    # which would wrongly restore an older gold.
    def _needs_rollback(dag_run, **_):
        published = dag_run.get_task_instance("publish").state == "success"
        prod_failed = dag_run.get_task_instance("prod_check").state == "failed"
        return published and prod_failed

    needs_rollback = ShortCircuitOperator(
        task_id="needs_rollback",
        python_callable=_needs_rollback,
        trigger_rule=TriggerRule.ALL_DONE,
        ignore_downstream_trigger_rules=True,
    )

    rollback = _bq("rollback", _clone_sql("gold", "gold_prev") + "\n"
                   + _log_sql("publish_log", "rollback"))

    # Leaves the run red after a rollback, so it alerts like any failure.
    fail_after_rollback = BashOperator(
        task_id="fail_after_rollback",
        bash_command='echo "prod check failed; gold rolled back to the previous publish"; exit 1',
    )

    dbt_build_staging >> mark_staging_built >> preprod_gate >> publish >> prod_check
    prod_check >> needs_rollback >> rollback >> fail_after_rollback
