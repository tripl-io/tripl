---
title: Connect your warehouse
sidebar_position: 2
description: Add a read-only connection to ClickHouse, BigQuery, Databricks or PostgreSQL and check that tripl can reach it.
---

# Connect your warehouse

**You will:** give tripl a read-only connection to the warehouse your analytics
events already land in.

**You need:** to be an **owner** or **admin** of the organization, and
credentials for a warehouse user that can read the events table. tripl only
reads: it never writes to the warehouse, and it keeps aggregated counts, not your
raw events.

## 1. Open Data sources

Open the settings (**Workspace settings** in the account menu at the bottom
left, or **Settings** in the sidebar of **All projects**) and choose **Data
sources** in the **Organization** group. A connection belongs to the
organization, so every project in it can scan from it.

![Settings → Data sources, with one healthy connection and the Add connection button](/img/screenshots/data-sources.light.webp#gh-light-mode-only)
![Settings → Data sources, with one healthy connection and the Add connection button](/img/screenshots/data-sources.dark.webp#gh-dark-mode-only)

## 2. Add the connection

Press **Add connection**, give it a name your team will recognise, and pick the
**Type**. The form asks for what that warehouse needs:

| Warehouse | What to fill in |
| --- | --- |
| **ClickHouse** | Host, port (8123), database, username, password. |
| **PostgreSQL** | Host, port (5432), database, username, password. Version 14 or newer. |
| **BigQuery** | GCP project ID, a default dataset, and a service-account JSON key pasted into the form. |
| **Databricks** | Server hostname, catalog, the SQL warehouse's HTTP path, and an access token (or a service principal's OAuth client ID and secret). See [Databricks](#databricks) below. |

![The New data source dialog for ClickHouse](/img/screenshots/data-source-add.light.webp#gh-light-mode-only)
![The New data source dialog for ClickHouse](/img/screenshots/data-source-add.dark.webp#gh-dark-mode-only)

Press **Test connection** before **Create**: it tries the credentials without
saving anything.

:::tip Give tripl its own read-only user
A warehouse user that can only `SELECT` from the events tables is all tripl needs.
It also makes tripl's queries easy to find in the warehouse's own query log.
:::

### Databricks

tripl queries a Databricks **SQL warehouse** (serverless, pro or classic) in a
Unity Catalog workspace. All-purpose clusters are not supported.

1. **Find the connection details.** In the workspace, open **SQL Warehouses**,
   pick the warehouse, and open **Connection details**. Copy the **Server
   hostname** (for example `dbc-a1b2c3d4-e5f6.cloud.databricks.com`, without
   `https://`) and the **HTTP path** (for example `/sql/1.0/warehouses/1234abcd`).
2. **Pick an identity.** Either:
   - a **personal access token** (**Settings → Developer → Access tokens**), or a
     token issued to a service principal. Choose **Access token** under
     **Authentication** and paste it into **Access token or OAuth secret**; or
   - a **service principal with OAuth machine-to-machine**. Create an OAuth
     secret for the service principal, choose **OAuth machine-to-machine** under
     **Authentication**, put its **client ID** in **OAuth client ID** and the
     **secret** in **Access token or OAuth secret**. tripl exchanges them for
     short-lived tokens at the workspace's `/oidc/v1/token` endpoint.
3. **Grant read-only access.** The identity needs `CAN USE` on the SQL warehouse
   and, in Unity Catalog, nothing more than:

   ```sql
   GRANT USE CATALOG ON CATALOG main TO `tripl-reader`;
   GRANT USE SCHEMA ON SCHEMA main.analytics TO `tripl-reader`;
   GRANT SELECT ON SCHEMA main.analytics TO `tripl-reader`;
   ```

   tripl only ever sends `SELECT` (and `DESCRIBE QUERY`) statements; its SQL gate
   refuses `INSERT`, `MERGE`, `COPY INTO`, `CREATE`/`ALTER`/`DROP`, `OPTIMIZE`,
   `VACUUM`, `SET`, `USE` and every other statement before it reaches the
   warehouse.
4. **Fill in the form.**

   | Field | What goes in it |
   | --- | --- |
   | **Server hostname** | The workspace hostname from step 1. |
   | **Catalog** | The Unity Catalog catalog queries and the schema browser use, for example `main`. |
   | **HTTP path** | The warehouse's HTTP path from step 1. Required. |
   | **Authentication** | **Access token** or **OAuth machine-to-machine**. |
   | **Default schema** | Where unqualified table names resolve. Empty means `default`. |
   | **Schema allowlist** | Other schemas of the catalog the schema browser may list, comma-separated. |
   | **Timeout (seconds)** | The per-statement budget. tripl sets it as the warehouse's `STATEMENT_TIMEOUT` and also cancels the statement itself when it runs over. |

   The port is always 443. Every session runs with its time zone set to UTC, so
   buckets and windows line up with the other warehouses.

A stopped serverless warehouse starts on the first query, which can take a few
seconds; set the timeout with that in mind. Queries tripl runs are billed as
warehouse time like any other.

## 3. Check it stays healthy

The new card shows the result of the last test: **Healthy** in green, a warning
in amber once that result is more than a week old, or the error in red with
**Re-test connection** and **Edit connection** beside it. A test only proves the
warehouse was reachable at that moment, so re-test after you rotate a password or
change the network.

## Next

A connection does nothing on its own. Next,
[draft your tracking plan from a scan](./draft-your-plan-from-a-scan.md), which
reads from it.

**More detail:** the [User Guide](../use/user-guide.md#connect-point-tripl-at-your-warehouse)
covers TLS modes, BigQuery cost guards, Databricks settings and query timeouts, and the
[warehouse capability matrix](../develop/warehouse-parity.md) says what each
warehouse supports and how well it is tested.
