---
title: Editions
sidebar_position: 2.4
---

# Editions

Tripl comes in two editions built from the same code.

**Community** is free and open source (AGPL-3.0-or-later). It is the whole
product for a team: tracking plans, scans of your warehouse, monitoring and
alerts, docs, the CLI and the MCP server, an organization with roles and groups,
each project's audit history, and Sign in with Google.

**Enterprise** adds what larger organizations need to run Tripl under their
own identity and compliance rules. It ships as a separate private image,
`ghcr.io/tripl-io/tripl-enterprise`, under a commercial license.

| Feature | Community | Enterprise |
|---|:---:|:---:|
| Tracking plans, scans, monitoring, alerts | ✓ | ✓ |
| One organization with roles, groups, API keys | ✓ | ✓ |
| More organizations on one instance, each with its own members and projects | | ✓ |
| Project audit history | ✓ | ✓ |
| Sign in with Google | ✓ | ✓ |
| Sign in through your own OpenID Connect provider (Okta, Entra ID, Keycloak, …), for the whole instance | ✓ | ✓ |
| Single sign-on per organization (OpenID Connect, SAML 2.0), verified domains, SSO required | | ✓ |
| Provisioning over SCIM 2.0, groups mapped to roles | | ✓ |
| Organization-wide audit log, search across projects, export (CSV, NDJSON) | | ✓ |
| Audit webhook to a SIEM | | ✓ |
| Audit log retention policies, legal hold | | ✓ |
| Project health across the organization, search across every project | | ✓ |
| Alert escalation policies and organization-wide alert routes | | ✓ |
| Per-project merge policy, event type owners, `tripl check` | ✓ | ✓ |
| Plan governance across projects: naming rules, required and forbidden properties, approval of sensitive fields, protected main | | ✓ |
| Group roles in projects, custom project roles, team sync of groups at single sign-on | | ✓ |
| Stored secrets under your own key management service (AWS KMS, Google Cloud KMS, Azure Key Vault, HashiCorp Vault), key rotation | | ✓ |
| Platform console: every organization and account, suspension, read-only step-in | | ✓ |
| Public demo instance (`PUBLIC_DEMO`), a pool of ready demos, purge of idle organizations | | ✓ |
| Multi-tenant hosted service (`DEPLOYMENT_MODE=hosted`): each sign-up gets its own organization, verified addresses | | ✓ |

The Enterprise features are documented in the **Enterprise** section of these
docs: [single sign-on and provisioning](./enterprise/sso-and-scim.md), the
[organization audit log](./enterprise/audit.md) with its export and webhook,
[project health and search across projects](./enterprise/org-insights.md),
[alert escalation](./enterprise/escalation.md),
[plan governance](./enterprise/governance.md),
[access control](./enterprise/rbac.md) beyond the fixed roles, the
[platform console](./enterprise/platform-console.md),
[key management](./enterprise/kms.md) for stored secrets, and the
[license key](./enterprise/license.md) an Enterprise instance runs under.

Every Enterprise feature is in the Enterprise image only: the Community image
does not contain its code.

A Community instance shows each Enterprise feature in Settings with an
**Enterprise** tag. Its page says what the feature does.

To install the Enterprise image, see `tripl install --edition enterprise` in
the [CLI reference](./run/cli.md). To get access, write to sales@tripl.io.
