---
title: Project templates
sidebar_position: 13
---

# Project templates

A new project doesn't have to start from a blank plan. **New project** offers
four industry templates next to **Blank project**:

| Template | What it covers | Example events |
| --- | --- | --- |
| **E-commerce** | Product discovery, cart, checkout, orders and refunds | `product_viewed`, `checkout_started`, `order_completed` |
| **Subscriptions** | Sign-up, paywall, free trial, renewals and churn | `sign_up_completed`, `trial_started`, `subscription_started` |
| **Mobile games** | Sessions, tutorial, levels, in-app purchases and ads | `tutorial_completed`, `level_completed`, `ad_impression` |
| **B2B SaaS** | Workspace activation, invitations, feature use and billing | `workspace_created`, `user_invited`, `plan_upgraded` |

A template is a starting point written with common analytics conventions:
snake_case names, `${variable}` placeholders, and one name and type for a field
that several templates share (`billing_period`, `failure_reason`, `placement`).
The consumer templates (E-commerce, Subscriptions, Mobile games) put a required
`platform` field on every event type; B2B SaaS keys its events by
`workspace_id` instead. Rename, add or delete anything before you merge it.

## What a template creates

Everything lands on a **draft branch** named after the template (for example
`template/ecommerce`), not on the live plan:

- **Event types** with their **fields**: types, required flags and enum options.
- **Properties** such as `${product_id}`, `${currency}` or `${platform}`, with
  documented values where the vocabulary is closed.
- **Example events**, every one with the status **draft**, each with a title, a
  description, tags and field values that reference those properties. Field
  values count as authored, as if you had typed them, so a later scan never
  rewrites a documented `${variable}`.

The project's **main plan stays empty** until you merge the branch. The
Overview's counters read zero until then, and nothing is compared against your
warehouse yet.

The project, its main branch, your membership and the template branch are
created in one step: if seeding fails, no project is created at all. You are the
branch's author and are subscribed to it, and the audit log records both the
project creation and the branch creation, each naming the template.

After creating the project, tripl opens the branch instead of the Overview so
you can review it right away. The branch **diff** lists every template row as an
addition.

## What each template contains

All four templates are at **version 1** and share the same shape: five event
types, a set of properties, draft example events, five starter-metric
suggestions and three starter-alert suggestions.

### E-commerce

Branch `template/ecommerce`: 5 event types, 24 fields, 4 properties, 9 events.

| Event type | Fields |
| --- | --- |
| `page_view` | `platform`, `page_type`, `page_path` |
| `product` | `platform`, `product_id`, `product_category`, `price`, `currency`, `quantity` |
| `cart` | `platform`, `cart_value`, `item_count`, `currency` |
| `checkout` | `platform`, `checkout_step`, `cart_value`, `currency`, `payment_method` |
| `order` | `platform`, `order_id`, `revenue`, `currency`, `payment_method`, `refund_reason` |

- **Properties:** `product_id`, `currency`, `order_id`, `platform`.
- **Events:** `page_viewed`, `product_list_viewed`, `product_viewed`,
  `product_added_to_cart`, `cart_viewed`, `checkout_started`,
  `payment_info_entered`, `order_completed`, `order_refunded`.
- **Starter metrics (suggested):** Checkout conversion, Add-to-cart rate,
  Orders, Refund rate (from a scan); Gross revenue (sum over an orders table in
  a data source).
- **Starter alerts (suggested):** volume drops on core funnel events, schema
  drift on checkout and order events, source freshness.

### Subscriptions

Branch `template/subscriptions`: 5 event types, 20 fields, 4 properties, 7 events.

| Event type | Fields |
| --- | --- |
| `account` | `platform`, `signup_method`, `referral_source` |
| `paywall` | `platform`, `paywall_id`, `placement`, `plan_count` |
| `trial` | `platform`, `plan_id`, `trial_length_days` |
| `subscription` | `platform`, `plan_id`, `billing_period`, `price`, `currency`, `cancel_reason` |
| `billing` | `platform`, `plan_id`, `failure_reason`, `retry_count` |

- **Properties:** `platform`, `plan_id`, `billing_period`, `currency`.
- **Events:** `sign_up_completed`, `paywall_viewed`, `trial_started`,
  `subscription_started`, `subscription_renewed`, `subscription_cancelled`,
  `payment_failed`.
- **Starter metrics (suggested):** Trial-to-paid conversion, Paywall
  conversion, Cancellations, Payment failure rate (from a scan); Renewal revenue
  (sum over a billing table in a data source).
- **Starter alerts (suggested):** volume drops on trial and subscription
  starts, a spike in failed payments, schema drift on subscription and billing
  events.

### Mobile games

Branch `template/mobile-games`: 5 event types, 20 fields, 5 properties, 8 events.

| Event type | Fields |
| --- | --- |
| `session` | `platform`, `app_version`, `session_number` |
| `onboarding` | `platform`, `app_version`, `duration_seconds` |
| `level` | `platform`, `level_id`, `attempt`, `duration_seconds`, `failure_reason` |
| `monetization` | `platform`, `product_sku`, `price`, `currency`, `store` |
| `ads` | `platform`, `ad_format`, `ad_network`, `placement` |

- **Properties:** `platform`, `app_version`, `level_id`, `product_sku`, `currency`.
- **Events:** `session_started`, `tutorial_started`, `tutorial_completed`,
  `level_started`, `level_completed`, `level_failed`, `in_app_purchase_completed`,
  `ad_impression`.
- **Starter metrics (suggested):** Tutorial completion rate (completed per
  started tutorial), Level win rate,
  Purchases, Ad impressions (from a scan); In-app purchase revenue (sum over a
  purchases table in a data source).
- **Starter alerts (suggested):** volume drops on sessions and level starts,
  volume drops on purchases and ad impressions, schema drift on level events
  after a client release.

### B2B SaaS

Branch `template/b2b-saas`: 5 event types, 17 fields, 4 properties, 7 events.

| Event type | Fields |
| --- | --- |
| `account` | `signup_method`, `company_size`, `platform` |
| `workspace` | `workspace_id`, `plan_id` |
| `collaboration` | `workspace_id`, `invitee_role`, `invite_channel` |
| `product_usage` | `workspace_id`, `feature_name`, `user_role` |
| `billing` | `workspace_id`, `plan_id`, `previous_plan_id`, `seats`, `billing_period`, `cancel_reason` |

- **Properties:** `workspace_id`, `user_role`, `plan_id`, `feature_name`.
- **Events:** `sign_up_completed`, `workspace_created`, `user_invited`,
  `invite_accepted`, `feature_used`, `plan_upgraded`, `subscription_cancelled`.
- **Starter metrics (suggested):** Workspace activation rate, Invite acceptance
  rate, Feature usage, Upgrade rate (from a scan); Expansion MRR (a SQL query on
  a data source).
- **Starter alerts (suggested):** volume drops on sign-ups and workspace
  creation, schema drift on billing events, source freshness.

## What a template does not create, and why

Each template also lists **starter metrics** (for example *Checkout conversion =
`order_completed` / `checkout_started`*) and **starter alert rules** (for example
*Volume drops on core funnel events*). These are **suggestions only**. You see
them under the New project picker once you choose the template, and as a
checklist in the branch description, but they are **not created automatically**:

- **Metrics need data.** A metric counts events from a scan or reads a table in
  a data source, and a brand-new project has neither.
- **Alert rules need a destination.** A rule sends to a
  [destination](./alerting.md#destinations) (Slack, Microsoft Teams, email, and
  the rest) with credentials only your team can supply.
- **No synthetic warehouse.** Unlike the [demo project](./demo-workspace.md),
  a template creates no data source, scan, metric history or alerts, so a real
  project never shows made-up numbers.
- **Metrics are not branched.** A metric definition lives outside the plan
  branches, so one created with the template could not be reviewed or dropped
  with the branch the way the plan is.

Set these up yourself after you merge the branch and
[connect a data source](../quick-start.md#step-4--connect-your-warehouse).

## Review and merge

The template branch uses the ordinary branch flow described in the
[user guide](./user-guide.md):

1. Open the branch (tripl takes you there after creating the project) and read
   the diff.
2. Edit it: rename events to match your naming, drop what you don't track, and
   add what's missing. Everything stays on the branch.
3. **Submit** it for review, get it **approved**, then **merge**. The event
   types, fields, properties and events become the project's main plan.

If you don't want the template at all, close the branch. Main was never
touched, so you are left with a blank project.

## Versioning

Each template has a **version** number that the picker's template card and the
branch description show. A template is applied once, when the project is created. Later
versions of a template never change a project that already exists: what you
merged is your plan. Improvements to a template only reach projects created
after the change.

## Over the API

`GET /api/v1/project-templates` lists the templates, and
`POST /api/v1/projects` with a `template_id` creates a project from one. See
[Creating a project from a template](../integrate/agent-api-guide.md#project-templates)
in the Agent API guide.
