# cube-semantic-tests

A pytest suite for a **Cube Cloud** semantic layer backed by **BigQuery**. It tests the parts dbt
tests can't see: what Cube exposes, who can see which rows, whether joins inflate totals, whether
measures return the right numbers, and whether pre-aggregations are built, correct and fresh.

The structure is adapted from the four-tier design in
[VitorFaccin/semantic-layer](https://github.com/VitorFaccin/semantic-layer), with its weaknesses
fixed:

| Fixed weakness in the original | How this suite fixes it |
|---|---|
| Tests ran through an admin fallback | Every call runs as a named persona with a signed, expiring token. There is no default token. |
| Hand-written lists of views to check for fan-out and row filters | Test targets come from `/meta`, so a new view is covered as soon as it's published. |
| Pre-aggregations only checked for routing | Checks routing, that the rollup was built and served, its values against BigQuery, and its freshness. |
| Parity tests used all-time totals only | Cases use bounded date ranges, time-bucketed groups, rolling windows, prior period and row-level security. |
| Correctness checked only against the author's own SQL | Golden numbers from an independent source come first, and each case must name an owner and a source. |
| PII detected by column name only | Uses `meta.pii` / `meta.classification` tags first, with name patterns as a backstop. |
| Distinct counts not flagged as non-additive | `countDistinct` and `countDistinctApprox` must declare `meta.additive: false`. |
| DuckDB-only SQL | BigQuery reference SQL, with a bytes-billed cap and labels on every job. |

## Layout

```
semtest/            client code: Cube REST + Control Plane, BigQuery runner, /meta helpers, comparison
config/             EDIT THESE: suite.yml, personas.yml, parity_cases.yml, rollup_cases.yml
tests/contract/     compile, storefront, governance, auth, row filter in generated SQL (no BigQuery cost)
tests/data/         smoke, row values, fan-out, parity, rollups (executes through Cube)
tests/unit/         the framework's own tests, against a fake Cube (no network, no credentials)
scripts/            lint_model.py (static model lint), wait_for_rollups.py
sql/                check_results.sql: DDL and example queries for the results table
ci/                 example GitHub Actions workflow
```

## Tiers

| Marker | Needs | Checks | When to run |
|---|---|---|---|
| `unit` | Nothing | The framework itself: config loading, row-limit detection, row-filter SQL parsing, rollup freshness logic. | Every change to `semtest/` or `tests/` |
| `contract` | Cube | Model compiles (Control Plane). Public views are exactly the expected list and no cube leaks. Descriptions, owner/steward meta, PII confinement, non-additive flags. Missing/forged/expired tokens are refused. Each persona sees exactly its views. The row filter is in the generated SQL, including when a segment is applied. | Every PR, against the branch's staging environment |
| `data` | Cube + BigQuery | Every measure and time dimension runs. Restricted personas get back only their allowed values. Joined dimensions don't inflate additive totals. | PR (on sandbox data) or after the production load |
| `parity` | Cube + BigQuery | Golden numbers and BigQuery reference SQL, including row-level security checked numerically | After the production dbt run |
| `rollup` | Cube + BigQuery | Rollup routing, that it was built and served, values, freshness | After the dbt run **and** the rollup refresh |

`scripts/lint_model.py` runs before all of these, with no Cube needed. It catches duplicate YAML
keys (PyYAML accepts them, Cube refuses them at boot), quoted Jinja interpolation, cubes that
aren't `public: false`, and cubes with no `primary_key`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in Cube URL/secret, Control Plane key, BigQuery project
pytest -m unit                # framework self-tests, no credentials needed
gcloud auth application-default login   # local BigQuery auth
python scripts/lint_model.py ../your-cube-repo/model
pytest -m contract
pytest -m "data or parity"
python scripts/wait_for_rollups.py && pytest -m rollup
```

## Configuring it for your model

1. **`config/suite.yml`**:
   - `expected_views`: your public views
   - `private_prefix`
   - the `meta` keys every view must carry
   - PII rules
   - `row_filters`: the dimension your access policies filter on
   - a short `smoke_date_range` to cap cost
2. **`config/personas.yml`**: one entry per access pattern. Each has `claims` (the JWT payload,
   which becomes the security context), `can_see`, `cannot_see` and `row_values`. Shape the
   claims to whatever your `cube.py` and `access_policy` rules read. The examples match a model
   like this:

   ```python
   # cube.py
   from cube import config

   @config('context_to_groups')
   def context_to_groups(ctx):
       return (ctx.get('securityContext') or {}).get('groups', [])
   ```

   ```yaml
   # model/views/revenue.yml
   views:
     - name: revenue
       meta: {owner: sales-analytics@yourco.example, steward: jo.citizen, data_product: sales}
       access_policy:
         - group: exec
           member_level: {includes: "*"}
         - group: analyst
           member_level: {includes: "*"}
           row_level:
             filters:
               - member: state
                 operator: equals
                 values: ["{ securityContext.state }"]
   ```
3. **`config/parity_cases.yml`**: start with 3–5 golden numbers your finance or product owners
   already trust, then add grouped and time-based cases. Write the reference SQL from the
   business definition, and have someone other than the measure's author write or review it.
4. **`config/rollup_cases.yml`**: one case per pre-aggregation that matters. Include a query
   that should hit the rollup, the matching BigQuery SQL, and a freshness rule.

## CI: pre-prod gate, then publish

The suite assumes two Cube Cloud deployments of the same model: **pre-prod**, reading
`gold_staging`, and **prod**, reading `gold`. Prod never serves data that has not passed the full
suite on pre-prod:

1. dbt builds `gold_staging` (not `gold`), and Airflow logs it in `ops.staging_build_log`.
2. **Pre-prod gate:** pre-prod's rollups rebuild (their `refresh_key` reads that log), then the
   full suite runs against pre-prod, with `SEMTEST_VAR_GOLD_DATASET=gold_staging` so the
   reference SQL reads the same data.
3. **Publish**, only if the gate passed: each gold table is cloned to `gold_prev`, `gold_staging`
   is cloned over `gold`, and a row goes into `ops.publish_log`.
4. **Prod check:** prod's rollups rebuild (their `refresh_key` reads `publish_log`), then the
   rollup routing, materialisation and freshness checks run on prod. On failure, gold is cloned
   back from `gold_prev`.

Model changes are gated separately: a PR runs `lint` and the `contract` tier against the
branch's staging environment before merge.

Files:
- `ci/airflow_dag_example.py`: the nightly flow as an Airflow DAG (a sketch to adapt).
- `ci/github-actions.yml`: the PR gate, plus `preprod-gate` and `prod-check` jobs that Airflow can
  trigger with `repository_dispatch` (`staging-built`, `gold-published`) if you'd rather run
  the suite in GitHub Actions.
- `sql/publish_gold.sql`: the publish log tables, publish and rollback SQL, and the Cube model
  settings (`GOLD_DATASET`, `REFRESH_LOG` and the `refresh_key`) for both deployments.

**Config that differs per environment** is set with environment variables, never by editing
cases: `CUBE_API_URL`, `CUBE_API_SECRET`, and `SEMTEST_VAR_<NAME>` for any `${name}` in the
reference SQL (`suite.yml › sql_vars`).

**Dates** in cases and `smoke_date_range` can be relative (`last_complete_month`,
`last_7_complete_days`, ...), so they stay inside the data, including a staging dataset that
only holds recent days. The resolved range is `${date_from}` / `${date_to}` in that case's SQL.
Keep golden numbers on fixed, closed periods. `SEMTEST_AS_OF=YYYY-MM-DD` re-runs a past day.

BigQuery access uses Workload Identity Federation. The service account needs BigQuery Job User
and read access to `gold` and `gold_staging`; the publish step also needs Data Editor on `gold`,
`gold_prev` and `ops`.

## Storing results

By default results only appear as pass/fail in the pytest output. Turn on the results table to
keep one row per check per run, so you can trend numbers, see what is failing right now and
who owns it, and alert from BigQuery (or Looker, or an Airflow sensor) instead of a CI log.

**Turn it on** in `config/suite.yml › results` (`enabled: true`), or per run with environment
variables, which win over the file:

```bash
SEMTEST_RESULTS=bigquery SEMTEST_RESULTS_TABLE=your-project.semantic_tests.check_results \
SEMTEST_ENVIRONMENT=prod pytest -m "data or parity or rollup"
SEMTEST_RESULTS=jsonl pytest -m contract        # local file: results/check_results.jsonl
```

The BigQuery sink appends with a load job (no streaming cost) using Application Default
Credentials, like the reference queries; no keys are configured in the repo. With
`auto_create: true` it creates the dataset and a table partitioned by day and clustered by
check and status, which needs BigQuery Data Editor on that dataset. Otherwise create it from
`sql/check_results.sql` (or `python -m semtest.results --ddl project.dataset.table`). If the
write fails, the run still reports its test results and prints the error. Set
`fail_on_write_error: true` to fail the run as well.

**Columns:** `run_id` (CI_RUN_ID or a generated id), `run_started_at`, `recorded_at`,
`check_id` (pytest node id), `check_name`, `case_id`, `tier`, `status`
(`pass | fail | warn | error | skip`), `severity` (`error | warn`), `observed`, `expected`
(JSON), `observed_value` / `expected_value` (when the value is a single number), `message`,
`owner`, `environment`, `duration_s`, `git_sha`.

**Owner** comes from a case's `owner:` (parity and rollup cases), else the view's `meta.owner`
for checks run per view, else whatever the test sets with `dq.owner = ...`.

**Severity.** Mark a test `@pytest.mark.severity("warn")`, or give a config case
`severity: warn`. A failing warn-level check is stored as `warn`, listed under "warn-level
checks that failed" at the end of the run, and does not fail the run. Everything else is `error`.

**Observed vs expected.** Parity, rollup values and freshness, fan-out and row-value checks
already record what they saw. In your own tests, take the `dq` fixture:

```python
def test_orders_volume(cube, default_claims, dq):
    got = ...
    dq.observe(observed=got, expected=1000)
    assert got >= 1000
```

**From Airflow**, run the suite in a task after the dbt run and the rollup refresh, with
`SEMTEST_RESULTS=bigquery`, `CI_RUN_ID={{ run_id }}` and `SEMTEST_ENVIRONMENT=prod`. A
downstream task can then read the latest statuses for the run, using the queries in
`sql/check_results.sql`.

## Assumptions to check against your deployment

The suite was exercised against a local fake of Cube's REST API (including planted row-filter,
fan-out, stale-rollup and fail-open-auth bugs, each caught by its test), **not** against a live
Cube Cloud deployment. Before relying on it, check:

- **Token claims.** Your `check_auth` / `context_to_groups` read the claim names used in
  `personas.yml`. If you use Cube Cloud SSO user attributes instead of JWTs, the personas need
  real test users, and the row filter may reference `userAttributes`.
- **`/meta` fields.** Entries carry `type: view|cube` and view members carry `aliasMember`.
  Fan-out pair generation relies on `aliasMember`. Without it, those tests skip.
- **Pre-aggregation fields.** `/v1/sql` returns a `preAggregations` list and `/v1/load` returns
  `usedPreAggregations` for your Cube version. The tests match the rollup name as a substring
  of those fields.
- **Staging URL.** The staging REST URL pattern for branch environments. Copy a real one from
  Cube Cloud into `CUBE_STAGING_API_URL_TEMPLATE`.
- **Time zones.** The `timezone` in each Cube query matches how the reference SQL buckets dates.
- **Cost.** Fan-out runs two queries per pair (capped by `max_pairs_per_view`, bounded by
  `smoke_date_range`). Every BigQuery reference query is capped by `BQ_MAX_BYTES_BILLED`.
