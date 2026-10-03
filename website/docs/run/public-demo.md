---
title: Running a public demo
sidebar_position: 8
---

# Running a public demo

A public demo is a tripl instance anyone can sign in to and try on generated
demo projects. Every visitor gets an organization of their own, and nothing
they do reaches outside the instance. This page lists the settings for one.
Each setting is described in [Configuration](./configuration.md).

## Settings

```bash
# Multi-tenant: each sign-up gets an organization of its own, and warehouse
# hosts must resolve to public addresses.
DEPLOYMENT_MODE=hosted
REGISTRATION_MODE=open
PLATFORM_ADMIN_EMAILS=you@example.com

# Sign in with Google: the only way to sign up on a public demo.
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...            # from your secret store, never the repo
# GOOGLE_ALLOWED_DOMAINS=           # empty: any Google account

# Refuse whatever would reach outside the instance; AI off.
PUBLIC_DEMO=true

# Delete organizations nobody has used for 14 days.
IDLE_ORG_RETENTION_DAYS=14

# Behind a TLS-terminating proxy.
APP_BASE_URL=https://demo.example.com
SESSION_COOKIE_SECURE=true
RATE_LIMIT_TRUST_FORWARDED_FOR=true   # only when the proxy sets X-Forwarded-For
```

Keep `DEMO_ENABLED` and `DEMO_RUNTIME_ENABLED` at their default (`true`): the
first lets a visitor generate a demo project, the second keeps its data fresh.

## Google client

In the Google Cloud console, create an OAuth client of type **Web
application**. Add `https://demo.example.com/api/v1/auth/google/callback`
(your `APP_BASE_URL` plus that path) as an **authorized redirect URI**. The
consent screen needs only the `openid`, `email` and `profile` scopes.

## What a visitor can and cannot do

A visitor signs in with Google, lands in an organization of their own, and
generates demo projects there. Scans, alerts, the catalog and the rest of the
app work on those projects as they would on real ones. Demo alert
destinations deliver to an in-instance sink, not to Slack or email.

With `PUBLIC_DEMO=true` the server refuses, with `403` and a reason:

- password sign-up (sign in with Google instead);
- adding, testing or editing a warehouse connection;
- creating a project other than a demo one, or another organization;
- sending invitations;
- changing organization settings (AI, SMTP, embeddings), single sign-on, SCIM,
  the audit webhook, and issue-tracker integrations.

AI features are off whatever the instance or organization settings say. The
app shows a banner saying this is a public demo and does not offer what the
server refuses.

## Cleaning up

`IDLE_ORG_RETENTION_DAYS` is checked once a night by `celery-beat`,
so beat must be running. An organization is deleted when it was created more
than that many days ago and, in that time, no member has signed in and no
demo project in it has been opened. Deletion is the same purge as an owner's
delete. The default organization is never deleted. A visitor who signs in
again later gets a fresh organization.

Accounts are kept when their organization goes; only the organization and its
data are removed.
