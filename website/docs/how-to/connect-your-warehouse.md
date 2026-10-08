---
title: Connect your warehouse
sidebar_position: 2
description: Add a read-only connection to ClickHouse, BigQuery, Databricks, Snowflake, Redshift, Greenplum or PostgreSQL and check that tripl can reach it.
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
| **Snowflake** | Account identifier, database, user, the virtual warehouse, and a password or the user's private key. See [Snowflake](#snowflake) below. |
| **Amazon Redshift** | Endpoint host, port (5439), database, username, password. Serverless and provisioned clusters. No JSON columns. See [Amazon Redshift](#amazon-redshift) below. |
| **Greenplum** | Coordinator host, port (5432), database, username, password. Greenplum 6 or 7, and the Cloudberry, Greengage and WarehousePG forks. See [Greenplum](#greenplum) below. |

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

### Snowflake

tripl runs its queries on a Snowflake **virtual warehouse** you name, as a user
you create for it.

1. **Find the account identifier.** In Snowsight, open the account menu →
   **Account** → **View account details** and copy the **Account identifier**
   (`myorg-myaccount`). The account locator with its region
   (`xy12345.eu-central-1.aws`) and the full
   `myorg-myaccount.snowflakecomputing.com` hostname work too.
2. **Create a read-only role and user.** Key-pair sign-in is recommended: it
   needs no password and is what Snowflake asks service users to use.

   ```sql
   CREATE ROLE tripl_reader;
   GRANT USAGE ON WAREHOUSE compute_wh TO ROLE tripl_reader;
   GRANT USAGE ON DATABASE analytics TO ROLE tripl_reader;
   GRANT USAGE ON SCHEMA analytics.events TO ROLE tripl_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA analytics.events TO ROLE tripl_reader;
   GRANT SELECT ON FUTURE TABLES IN SCHEMA analytics.events TO ROLE tripl_reader;

   CREATE USER tripl_reader TYPE = SERVICE DEFAULT_ROLE = tripl_reader
     RSA_PUBLIC_KEY = 'MIIBIjANBgkqh...';
   GRANT ROLE tripl_reader TO USER tripl_reader;
   ```

   Make the key pair with
   `openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt`
   and `openssl rsa -in rsa_key.p8 -pubout`; the public key goes into
   `RSA_PUBLIC_KEY` without its `-----BEGIN`/`-----END` lines. Encrypted private
   keys are not supported.

   tripl only ever sends `SELECT` statements (and asks Snowflake to describe a
   query without running it); its SQL gate refuses every other statement, and
   `SYSTEM$` functions, before they reach the warehouse.
3. **Fill in the form.**

   | Field | What goes in it |
   | --- | --- |
   | **Account identifier** | From step 1. |
   | **Database** | The database queries and the schema browser use, for example `ANALYTICS`. |
   | **User** | The user from step 2. |
   | **Password or private key** | The password, or, for key-pair sign-in, the whole PEM private key (`-----BEGIN PRIVATE KEY-----` …). |
   | **Warehouse** | The virtual warehouse queries run on. Required. An X-Small one is enough. |
   | **Role** | The role to use. Empty means the user's default role. |
   | **Authentication** | **Password** or **Key pair**. |
   | **Default schema** | Where unqualified table names resolve. Empty means `PUBLIC`. |
   | **Schema allowlist** | Other schemas of the database the schema browser may list, comma-separated. |
   | **Timeout (seconds)** | The per-statement budget. tripl sets it as `STATEMENT_TIMEOUT_IN_SECONDS` and also cancels the statement itself when it runs over. |

   The port is always 443. Every session runs with its time zone set to UTC, so
   buckets and windows line up with the other warehouses, and its queries carry
   the query tag `tripl`.

Snowflake upper-cases unquoted names, so a column written `event_name` in the
base query comes back as `EVENT_NAME`, and that is how scans and metrics refer to
it. A suspended warehouse resumes on the first query; queries tripl runs are
billed as warehouse time like any other, so let the warehouse auto-suspend.

### Amazon Redshift

tripl connects to a Redshift **Serverless workgroup** or a **provisioned
cluster** over Redshift's PostgreSQL-compatible endpoint, with a database user
and password.

1. **Find the endpoint.** For Serverless, open the workgroup and copy its
   **Endpoint** (`wg.123456789012.eu-west-1.redshift-serverless.amazonaws.com`);
   for a cluster, the cluster's **Endpoint**. Put the host part in **Host** and
   the port (5439 unless you changed it) in **Port**. The endpoint must be
   reachable from tripl: publicly accessible, or in a network tripl can route to,
   with the security group allowing the port.
2. **Create a read-only user.**

   ```sql
   CREATE USER tripl_reader PASSWORD '...';
   GRANT USAGE ON SCHEMA analytics TO tripl_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO tripl_reader;
   ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO tripl_reader;
   ```

3. **Fill in the form** like PostgreSQL's: host, port, database (`dev` by
   default), username, password. **SSL mode** defaults to `require` for a remote
   host; **Search path** names the schemas unqualified table names resolve in.

Every session runs with its time zone set to UTC. Redshift has no read-only
session switch like PostgreSQL's, so the user's own privileges are what keep
tripl from writing — grant `SELECT` and nothing more.

:::note Redshift sources have no JSON columns
A `SUPER` column is read as an opaque value: tripl does not list its paths,
track its properties or parse a text column as JSON on Redshift. Scans, metrics,
breakdowns and field contracts on ordinary columns work as on PostgreSQL.
:::

### Greenplum

tripl connects to the Greenplum **coordinator** (master) like any PostgreSQL
server. Greenplum 6 and 7 are supported, and so are the forks built on them —
Apache Cloudberry, Greengage and WarehousePG.

Fill in the form like PostgreSQL's: host, port (5432), database, username,
password, and optionally **SSL mode** and **Search path**. A read-only role is
enough:

```sql
CREATE ROLE tripl_reader LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA analytics TO tripl_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO tripl_reader;
```

Every session runs in UTC and read-only, exactly as on PostgreSQL. Greenplum 6
is built on PostgreSQL 9.4, so a regex field contract that uses lookbehind
(`(?<=...)`) is not evaluated there; the contracts beside it still run.

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
covers TLS modes, BigQuery cost guards, Databricks and Snowflake settings and query timeouts, and the
[warehouse capability matrix](../develop/warehouse-parity.md) says what each
warehouse supports and how well it is tested.
