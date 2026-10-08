"""Executable warehouse conformance gates.

Every other adapter test in this repo asserts generated SQL *strings* against
*fake* clients. A fake client accepts any string, which is how BigQuery shipped
``TIMESTAMP_BIN`` — a function GoogleSQL does not have — in every bucket query
with a green test suite, and how a ``GROUP BY <ARRAY>`` (which GoogleSQL rejects
outright) sat in every BigQuery JSON scan.

The suites in this package close that hole by making CI actually run the SQL:

* ``test_postgres_conformance``   — real ``postgres:18``, executes the SQL.
* ``test_clickhouse_conformance`` — real ``clickhouse-server``, executes the SQL.
* ``test_bigquery_analysis``      — real ZetaSQL analyzer via the credential-free
  ``bigquery-emulator``; asserts every generated statement *analyzes*.
* ``test_bigquery_value_conformance`` — real BigQuery on trusted release tags;
  asserts exact computed values from a typed, table-less fixture.
* ``test_bigquery_pipeline_value_conformance`` — the production scan, replay,
  catalog-metric and anomaly worker paths against real BigQuery plus PostgreSQL.
* ``test_databricks_value_conformance`` — a real Databricks SQL warehouse, run
  by hand with credentials (no CI job has one); asserts exact computed values
  from a typed, table-less fixture.
* ``test_snowflake_value_conformance`` — a real Snowflake account, on release
  tags when one is configured, or by hand; the same table-less approach.
* ``test_redshift_value_conformance`` — real Amazon Redshift (Serverless or a
  cluster), on release tags once configured, or by hand; same table-less approach.

* ``test_trino_value_conformance`` — a real Trino coordinator in Docker, on every
  pull request (credential-free); asserts exact computed values from a typed,
  table-less fixture shared with Athena (``trino_values``).
* ``test_athena_value_conformance`` — real Amazon Athena (engine version 3, i.e.
  Trino SQL), on release tags once configured, or by hand; the same assertions.

The PostgreSQL gates also run against Greenplum 6 and 7 (``TRIPL_CONF_PG_ENGINE=
greenplum``), through ``GreenplumAdapter``.

The contract they all measure against is :mod:`tripl.core.bucketing`.
"""
