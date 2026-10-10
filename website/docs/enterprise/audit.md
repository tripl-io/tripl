---
title: Organization audit log
sidebar_position: 2
---

# Organization audit log

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md). A project's own audit history is part of Community: its **Govern › Audit log** tab and `GET /api/v1/projects/{slug}/audit`.
:::

## Exporting the audit log {#audit-export}

Owners and admins of an organization can download its audit log as a file, for
an auditor, a spreadsheet or a one-off import into a SIEM. Open **Settings →
Organization → Audit log**, pick a format and a date range in the **Export**
card, and select **Export**. The browser downloads the file directly; nothing is
held in the page, so a large export does not slow it down.

- **What is included.** Every entry recorded in the organization, and every
  entry of its projects. Entries of other organizations and platform-level
  entries that belong to no organization (such as `platform.admin_grant`) are
  never included.
- **Range.** In the **Export** card, **From** and **To** are calendar days in
  UTC and **both are included**: 1 to 30 September exports all of 30
  September, and the default range ends with today. One export covers at most
  **366 days**; export a longer period in parts. The audit log page itself
  shows times, day headings and its own From and To filter in your browser's
  time zone, so an export's day boundaries can differ from the page's by your
  UTC offset.
- **Range in the API.** `from` and `to` are dates (`YYYY-MM-DD`) or ISO 8601
  timestamps, read in UTC, and the range is **`from` included, `to`
  excluded**: `to=2026-09-30` stops at midnight at the start of 30 September.
  To include a whole last day, send the day after it (the Export card does
  this for you). `to` must be after `from` and at most 366 days later;
  anything else is refused with `422`.
- **Formats.** **CSV** (`text/csv`, every cell quoted) or **NDJSON**
  (`application/x-ndjson`, one JSON object per line). The file is named
  `audit-<org>-<from>-<to>` with the format's extension.
- **Columns.** `id`, `created_at` (ISO 8601, UTC), `org_slug`, `project_slug`,
  `branch_name`, `user_email`, `action`, `target_type`, `target_id`,
  `target_name` and `payload`. In CSV `payload` is a JSON string; in NDJSON it
  is an object. Rows are ordered by `created_at`, then `id`.
- **Spreadsheet safety.** A CSV cell that starts with `=`, `+`, `-`, `@`, a tab
  or a carriage return gets a leading apostrophe (`'`), so a spreadsheet shows
  it as text instead of running it as a formula. NDJSON values are unchanged.
- **Streaming.** The server reads the log in pages of 1,000 entries and streams
  them as it goes, so an export of a busy year does not have to fit in memory on
  either side.
- **Filter.** The API also takes `action=` to export one action only.

The endpoint is `GET /api/v1/orgs/{org}/audit/export?format=csv|json&from=&to=`.
Like the audit feed, it takes an owner's or admin's **browser session**; an API
key is refused with `403`, whatever its scope. It is rate-limited to a few
exports a minute (`429` with `Retry-After` past that). Each export is itself
recorded as `org.audit_export`, with the format and the range.

## Audit webhook {#audit-webhook}

An organization can stream its audit log to a SIEM or log pipeline as it is
written: every new entry of the organization and its projects is POSTed, signed,
to one HTTPS endpoint. Set it up under **Settings → Organization → Audit
webhook**. Only an organization **owner** can see or change it (an admin gets a
notice; the API answers `403`), and it takes a browser session.

### Set it up

1. Enter the receiver's **URL** and save. It must be `https://`, and with
   `OUTBOUND_PUBLIC_HOSTS_ONLY=true` or on a hosted instance it must resolve to
   a public address: private, loopback and
   link-local addresses are refused when you save and again on every delivery.
2. The first save generates a **signing secret** and shows it **once**. Copy it
   into your receiver. tripl stores it encrypted and never shows it again; the
   page only says whether one is configured.
3. Select **Send test event**. tripl sends a synthetic `audit.webhook_test`
   event straight away and shows the status code your receiver answered (or why
   none came back). Tests and saves are rate-limited to 10 a minute.

**Rotate secret** generates a new secret and shows it once; deliveries are
signed with the new secret from then on, so update the receiver right away.
Turning **Send audit entries** off pauses the webhook: entries recorded while
it is off are not sent later. **Delete webhook** stops deliveries. The page also shows the last successful delivery, the last
error and a table of recent deliveries with their status.

### Payload

One JSON object per audit entry, `Content-Type: application/json`, with the same
fields as the [export](#audit-export) (`payload` is an object):

```json
{
  "id": "5f1c2a9e-3b4d-4e8f-9a61-0c7d2b3e4f50",
  "created_at": "2026-09-28T09:14:03.512000Z",
  "org_slug": "acme",
  "project_slug": "web",
  "branch_name": "",
  "user_email": "alex@example.com",
  "action": "org.member_role_update",
  "target_type": "user",
  "target_id": "8a2b6c1d-7e3f-4a5b-9c0d-1e2f3a4b5c6d",
  "target_name": "sam@example.com",
  "payload": { "before": "member", "after": "admin" }
}
```

`created_at` is ISO 8601 in UTC with microseconds (`.512000Z`), or with no
fraction when it is zero; parse it as a timestamp rather than matching its
length. A field with no value (`project_slug`, `branch_name`, `user_email`,
`target_name`) is an empty string, not `null`; `target_id` is `null` when the
entry has no target.

Every request carries three headers:

| Header | Value |
|---|---|
| `X-Tripl-Event-Id` | The audit entry's `id`. Use it to drop duplicates: delivery is at least once. |
| `X-Tripl-Timestamp` | When the request was signed, in Unix seconds. |
| `X-Tripl-Signature` | `sha256=` followed by the hex HMAC-SHA256 of `t=<timestamp>.<raw body>` (the literal `t=`, the `X-Tripl-Timestamp` value, a dot, then the body bytes), keyed with the signing secret. |

### Verify the signature

Compute the HMAC over `t=`, the timestamp, a dot and the **raw** request body
exactly as received (not a re-serialized copy), compare it in constant time,
and reject a timestamp more than a few minutes old so a captured request cannot
be replayed.

Python:

```python
import hashlib
import hmac
import time


def verify(secret: str, body: bytes, timestamp: str, signature: str, tolerance: int = 300) -> bool:
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    signed = b"t=" + timestamp.encode() + b"." + body
    expected = "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Node.js:

```js
const crypto = require('node:crypto')

function verify(secret, rawBody, timestamp, signature, tolerance = 300) {
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > tolerance) return false
  const expected =
    'sha256=' +
    crypto.createHmac('sha256', secret).update(`t=${timestamp}.`).update(rawBody).digest('hex')
  const a = Buffer.from(expected)
  const b = Buffer.from(signature)
  return a.length === b.length && crypto.timingSafeEqual(a, b)
}
```

In Express, read the body with `express.raw({ type: 'application/json' })` so
`rawBody` is the exact bytes that were signed.

### Delivery and retries

A background task picks up new entries about every 30 seconds. A delivery
succeeds when the receiver answers **2xx** within **15 seconds** (no single
network step may stall for more than 10). Only the status counts: tripl does
not read the response body, so answer as soon as the event is stored. Anything
else counts as a failure: another status, a timeout, a connection error, and a
**redirect** (redirects are never followed). A failed delivery is retried after
**1 minute, 5 minutes, 30 minutes, 2 hours**, then every **6 hours**; after
**8 failed attempts** it is marked **dead** and not tried again. Entries are
delivered independently, so they can arrive out of order: sort by `created_at`
if order matters.

While the organization is suspended or being deleted, nothing is delivered.
Delivered entries are removed from the delivery queue after 7 days and dead ones
after 30 days; the audit log itself is not affected, and the
[export](#audit-export) remains the way to fill a gap.

### Webhook audit

Every change to the webhook is recorded in the organization's audit log as
`org.audit_webhook.*` (creating, changing, deleting, rotating the secret and
sending a test), never with the secret.

## Audit retention {#audit-retention}

By default an organization keeps its audit log forever. Owners can set how long
entries are kept; older entries are then deleted once a day. A **legal hold**
keeps every entry, whatever the retention says, until it is turned off.

Open **Settings → Organization → Audit retention**.

- **Keep entries for.** A number of days between **30** and **3650** (ten
  years). Leave it blank to keep entries forever, which is the default.
- **Legal hold.** While it is on, nothing is deleted. Turn it on while a
  dispute, audit or investigation needs the whole trail; the retention you set
  applies again once it is off.

When you save a retention that will delete entries, the page asks you to
confirm first.

### What is deleted, and when

- Once a day, every entry of the organization and its projects whose time is
  older than the retention is deleted, together with any
  [audit webhook](#audit-webhook) deliveries still queued for it. Deletion
  cannot be undone.
- Only organizations that set a retention are affected. Entries recorded
  outside any organization (platform-level entries such as
  `platform.admin_grant`) are never deleted by a retention policy.
- Turning on legal hold stops a deletion that is already running before its
  next batch.
- To keep a copy, [export the audit log](#audit-export) before you shorten the
  retention, or stream it to your SIEM with the [audit webhook](#audit-webhook).
  An automatic "export before delete" is not available yet.

### Who can see and change it

| | Owners | Admins | Members | API keys |
|---|---|---|---|---|
| Read the policy | yes | yes | no | no |
| Change the policy | yes | no | no | no |

Both need a browser session; any API key is refused with `403`. A user who is
not in the organization gets `404`.

Every change is recorded in the audit log as `org.audit_retention.update`,
with the policy before and after.

### API

| Method | Path | Who |
|---|---|---|
| `GET` | `/api/v1/orgs/{org}/audit/retention` | owners and admins, browser session |
| `PUT` | `/api/v1/orgs/{org}/audit/retention` | owners, browser session |

`GET` answers:

```json
{
  "retention_days": 365,
  "legal_hold": false,
  "updated_at": "2026-10-05T09:30:00Z",
  "min_retention_days": 30,
  "max_retention_days": 3650
}
```

`retention_days` is `null` and `updated_at` is `null` when no policy was ever
saved. `PUT` takes both fields, `{"retention_days": 365, "legal_hold": false}`
(`retention_days` may be `null`), and answers like `GET`. A retention outside
30..3650, a missing field or an unknown one is refused with `422`.

## Security

From the Community security page (`run/security.md`).

### Audit webhook {#security-audit-webhook}

An organization owner can have every new audit entry POSTed to an HTTPS
endpoint (see [Audit webhook](#audit-webhook)).
The controls that matter for security:

| Property | Behaviour |
|---|---|
| Who configures it | Organization **owners** only, from a browser session. Admins, members and API keys get `403`. Every change is audited (`org.audit_webhook.*`) without the secret. |
| URL | Stored encrypted like the secret (with `ENCRYPTION_KEY`, or under the key service `KMS_PROVIDER` names) and returned only to owners; its host is kept apart for the audit rows. A webhook saved before this release keeps its URL in plain text until it is saved again. `GET` answers `url: ""` when the stored URL cannot be decrypted (save it again), and the test then reports `URL unreadable`. |
| Signing secret | Generated by the server when the webhook is created or the secret rotated, shown once in that response, and stored encrypted (with `ENCRYPTION_KEY`, or under the key service `KMS_PROVIDER` names; see [Key management](./kms.md)). Reads only say whether one is configured. |
| Signature | `X-Tripl-Signature: sha256=<hex>` is the HMAC-SHA256 of `t=<X-Tripl-Timestamp>.<raw body>` (the literal `t=` included). Receivers should compare in constant time, reject stale timestamps against replay, and drop duplicates by `X-Tripl-Event-Id` (delivery is at least once). |
| Outbound requests | `https` URLs only, no redirects followed (a redirect is a failed delivery), a 10-second timeout per network step and a 15-second deadline for the whole request, enforced even against a receiver that answers a byte at a time; the response body is never read. The owner's test and saves are rate-limited (10 a minute). With `OUTBOUND_PUBLIC_HOSTS_ONLY=true` or on a hosted instance a host that resolves to a private, loopback or link-local address is refused when the URL is saved and again at every delivery, and the request connects to the very address that was checked, as for single sign-on, so DNS rebinding cannot reach an internal address. |
| Queue | An entry is queued in the same database transaction that records it, so a committed entry is never lost to a crash and a rolled-back one is never sent. Deliveries of a suspended organization are held. While the key service is out of reach and the webhook's URL or secret needs a new unwrap, only that organization's deliveries are held back, a minute at a time with no attempt counted; every other organization's go on. A delivery run leases the rows it claims for longer than a run may last, and writes an outcome only while the row still carries its lease, so two runs never overwrite each other's results. |
| Retention | Delivered queue rows are deleted after 7 days, dead ones after 30 days. The audit log itself is untouched. |

Route gates, from the stricter-surfaces table:

| Surface | Gate | Why |
|---|---|---|
| `GET /api/v1/audit`, `GET /api/v1/audit/{entry_id}`, `GET /api/v1/audit/actions` | Org owner or admin | The list carries no payload; a payload is read one entry at a time from the detail route, behind the same gate. A payload re-exposes both of the rows above: `data_source.*` payloads carry the connection details blanked on a direct read, and `scan_config.create` payloads carry `base_query`. It is scoped to the request's organization: the list and the detail read only that organization's entries, so one organization's admin never reads another's payloads. Within it, `project_slug` is a filter, not a scope, and **Settings → Organization → Audit log** (Enterprise) is the org owner/admin screen that reads it that way: the actions belonging to no project (`data_source.*`, `user.*`, workspace `api_key.*`) answer nowhere else. That filter resolves the slug to a project and matches on its id, so a renamed project keeps one trail and a re-used slug inherits nobody's; while no live project answers to a slug, the denormalized label is matched instead, which is what keeps a deleted project's entries readable. Passwords were always redacted (`audit_service._redact`). |
| `GET /api/v1/orgs/{org}/audit/export` | Org owner or admin, interactive session (no API key) | The same rows the feed reads, with their payloads, as one file: the organization's entries and its projects', never another organization's or the platform's own. At most 366 days per request, streamed in pages of 1,000 rows, each page read in its own short transaction so a slow download holds no database connection; rate-limited to a few exports a minute. CSV cells starting with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading apostrophe against spreadsheet formula injection. Each export is audited as `org.audit_export`. See [Exporting the audit log](#audit-export). |
| `/api/v1/orgs/{org}/audit/webhook` and its `rotate-secret`, `test` and `deliveries` routes | Organization **owner**, interactive session | Where the whole audit trail is sent, so an owner's alone, like single sign-on. See [Audit webhook](#audit-webhook). |
| `GET` / `PUT /api/v1/orgs/{org}/audit/retention` | Read: org owner or admin; change: organization **owner**. Interactive session | How long the trail is kept, and the legal hold that keeps all of it: deleting history is an owner's decision. See [Audit retention](#audit-retention). |

## API reference

From the Community agent API guide (`integrate/agent-api-guide.md`).

The audit log leaves tripl two ways (see
[Exporting the audit log](#audit-export) and
[Audit webhook](#audit-webhook)). Neither takes an
API key: the export is an owner's or admin's browser session, like the audit
feed, and the webhook an owner's:

| Method and path | Who | What |
|---|---|---|
| `GET /api/v1/orgs/{org}/audit/export?format=csv\|json&from=YYYY-MM-DD&to=YYYY-MM-DD&action=` | owner or admin, browser session | A streamed download (`Content-Disposition: attachment`, `audit-<org>-<from>-<to>.<ext>`) of the organization's entries and its projects', ordered by `created_at` then `id`. `csv` is `text/csv` with every cell quoted and formula-like cells (`=`, `+`, `-`, `@`, tab, carriage return) prefixed with `'`; `json` is NDJSON (`application/x-ndjson`), one object per line. Columns: `id`, `created_at`, `org_slug`, `project_slug`, `branch_name`, `user_email`, `action`, `target_type`, `target_id`, `target_name`, `payload`. The range is `from` **included**, `to` **excluded** (both read in UTC; a bare date is midnight), so to include a whole last day send the day after it. `to` must be after `from` and at most 366 days later, else `422`. `action` is optional. Rate-limited (`429`). Audited as `org.audit_export`. |
| `GET /api/v1/orgs/{org}/audit/webhook` | owner, browser session | Always `200`: `configured`, `url`, `enabled`, `secret_configured` (the secret itself is never returned), `last_success_at`, `last_error`, `last_error_at`. With no webhook, `configured` is `false`, `url` is `""` and `enabled` `false`. |
| `PUT /api/v1/orgs/{org}/audit/webhook` | owner, browser session | `{"url", "enabled"}`. The URL must be `https`, and with `OUTBOUND_PUBLIC_HOSTS_ONLY=true` or on a hosted instance a public address (`422` otherwise). Answers the webhook's fields; the call that creates it also generates the signing secret and returns it once, in `secret`. Rate-limited with `test` (10 a minute, `429`). |
| `DELETE /api/v1/orgs/{org}/audit/webhook` | owner, browser session | Removes the webhook; nothing more is sent. |
| `POST /api/v1/orgs/{org}/audit/webhook/rotate-secret` | owner, browser session | The webhook's fields (as `GET`) plus `secret`: a new signing secret, returned once. The old one stops working at once. `404` when there is no webhook. |
| `POST /api/v1/orgs/{org}/audit/webhook/test` | owner, browser session | Sends a synthetic `audit.webhook_test` event now: `{"ok", "status_code", "error"}`. At most 15 seconds; rate-limited (10 a minute, `429`). |
| `GET /api/v1/orgs/{org}/audit/webhook/deliveries?status=&limit=` | owner, browser session | Recent deliveries, newest first: `id`, `audit_log_id`, `action`, `status` (`pending`, `sent`, `failed`, `dead`), `attempts`, `next_attempt_at`, `last_error`, `created_at`, `sent_at`. |

Each delivery is a `POST` of one entry as JSON with `X-Tripl-Event-Id` (the
entry's `id`), `X-Tripl-Timestamp` (Unix seconds) and
`X-Tripl-Signature: sha256=<hex HMAC-SHA256 of "t=<timestamp>.<raw body>">`
(the literal `t=`, then the `X-Tripl-Timestamp` value, a dot and the body bytes).
Webhook changes are audited as `org.audit_webhook.*`, without the secret.
