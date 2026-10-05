---
title: License key
sidebar_position: 4
---

# License key

:::info Enterprise
This page describes a feature of the [Enterprise edition](../editions.md).
:::

An Enterprise instance runs under a license key: who it is for, until when,
and for how many accounts. The key is checked offline, against a public key
built into the package, and **it never switches anything off**. With no key,
or an invalid, expired or over-seats one, every Enterprise feature keeps
working; the instance says what is wrong instead:

- **Settings › Platform › License** shows the state, licensee, expiry and
  seats in use;
- a banner across the app tells platform admins (nobody else) and links there;
- the server log says it once at startup, for a key set in `LICENSE_KEY`.

## Installing a key

Two places, and the first wins:

1. **`LICENSE_KEY`** in the server's environment (Compose passes it through).
   The License page then shows the key read-only; change it in the environment
   and restart. `PUT` and `DELETE /api/v1/platform/license` answer 409
   `ENV_MANAGED`.
2. **The License page.** A platform admin pastes the key and saves. It is
   verified before it is stored (a key tripl did not sign is refused with 422);
   an expired one is stored, and the page says it expired. It is stored
   encrypted with `ENCRYPTION_KEY`, in the operator-level `app_settings` row
   `enterprise_license`.

Installing and removing a key are audited as `platform.license_set` and
`platform.license_clear`, with the license id, licensee, expiry and seats —
never the key.

`GET /api/v1/platform/license` (platform admins) returns the status:
`state` is `valid`, `missing`, `invalid`, `expired` or `over_seats`, with a
`message` saying what is wrong.

## Seats

Seats count the instance's user accounts. Going over only warns.

## The key format

```
tripl1.<claims>.<signature>
```

`<claims>` is base64url (no padding) of a JSON object:

| Claim      | Meaning                                          |
|------------|--------------------------------------------------|
| `v`        | Format version, `1`.                             |
| `id`       | The license's id, for support and the audit log. |
| `edition`  | `enterprise`.                                    |
| `licensee` | Who it is issued to.                             |
| `issued`   | Issue date, `YYYY-MM-DD`.                        |
| `expires`  | Last valid day, `YYYY-MM-DD` (UTC).              |
| `seats`    | How many accounts it covers.                     |

`<signature>` is base64url of the Ed25519 signature over `tripl1.<claims>`.
The public key that checks it ships in the Enterprise package.

## Getting a key

Keys are issued by tripl. To get one, renew one or change its seats, contact
the tripl team; the new key replaces the old one in either place above.
