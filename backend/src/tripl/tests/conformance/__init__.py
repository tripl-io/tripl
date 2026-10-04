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

The contract they all measure against is :mod:`tripl.core.bucketing`.
"""
