-- Write, audit, publish for gold: promote gold_staging to gold only after the pre-prod gate passes.
--
-- Run by Airflow (ci/airflow_dag_example.py) as three separate steps. Replace your-project, the
-- dataset names and the table list with your own; repeat each CLONE line per gold table.
-- Table clones are copy-on-write: near-instant, and storage is billed only for changed data.
-- Check against your project before relying on it: CLONE needs the source and target datasets in
-- the same region, and the service account needs BigQuery Data Editor on gold, gold_prev and ops.

-- ------------------------------------------------------------------ once: the publish log
-- Prod's rollups use this as their refresh_key, so they rebuild only after a publish (never
-- while dbt is half way through, and never from data the gate has not passed).
CREATE TABLE IF NOT EXISTS `your-project.ops.publish_log` (
  published_at TIMESTAMP NOT NULL,
  run_id STRING NOT NULL,          -- the Airflow run that published
  action STRING NOT NULL           -- publish | rollback
);
-- Same shape; Airflow adds a row after dbt builds gold_staging (pre-prod's refresh_key).
CREATE TABLE IF NOT EXISTS `your-project.ops.staging_build_log` (
  published_at TIMESTAMP NOT NULL,
  run_id STRING NOT NULL,
  action STRING NOT NULL           -- built
);

-- ------------------------------------------------------------------ step 1: publish
-- Keep the current gold as gold_prev (for rollback), then replace gold with the tested staging.
-- BigQuery does not allow DDL (CREATE ... CLONE) inside a transaction, so this runs as a plain
-- script: each table switches on its own, a second or so apart. The log row is written last, so
-- prod rollups rebuild only once every table has switched.
CREATE OR REPLACE TABLE `your-project.gold_prev.fct_orders` CLONE `your-project.gold.fct_orders`;
CREATE OR REPLACE TABLE `your-project.gold.fct_orders` CLONE `your-project.gold_staging.fct_orders`;
-- ... one pair per gold table ...
INSERT INTO `your-project.ops.publish_log` VALUES (CURRENT_TIMESTAMP(), @run_id, 'publish');

-- ------------------------------------------------------------------ step 2 (on prod-check failure): rollback
-- CREATE OR REPLACE TABLE `your-project.gold.fct_orders` CLONE `your-project.gold_prev.fct_orders`;
-- -- ... one per gold table ...
-- INSERT INTO `your-project.ops.publish_log` VALUES (CURRENT_TIMESTAMP(), @run_id, 'rollback');

-- ------------------------------------------------------------------ Cube model (both deployments)
-- The same model runs in both deployments; environment variables pick the dataset and the log.
--   pre-prod: GOLD_DATASET=gold_staging  REFRESH_LOG=your-project.ops.staging_build_log
--   prod:     GOLD_DATASET=gold          REFRESH_LOG=your-project.ops.publish_log
--
--   cubes:
--     - name: base_orders
--       sql_table: "your-project.{{ env_var('GOLD_DATASET') }}.fct_orders"
--       pre_aggregations:
--         - name: revenue_by_state_monthly
--           refresh_key:
--             sql: "SELECT MAX(published_at) FROM `{{ env_var('REFRESH_LOG') }}`"
--             every: 5 minute
--
-- staging_build_log has the same shape; Airflow inserts a row after dbt builds gold_staging, so
-- pre-prod rollups rebuild once per nightly build rather than while dbt is still writing.
