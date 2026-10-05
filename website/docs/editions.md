---
title: Editions
sidebar_position: 2.4
---

# Editions

Tripl comes in two editions built from the same code.

**Community** is free and open source (AGPL-3.0-or-later). It is the whole
product for a team: tracking plans, scans of your warehouse, monitoring and
alerts, docs, the CLI and the MCP server, organizations with roles and groups,
the audit log, and Sign in with Google.

**Enterprise** adds what larger organizations need to run Tripl under their
own identity and compliance rules. It ships as a separate private image,
`ghcr.io/tripl-io/tripl-enterprise`, under a commercial license.

| Feature | Community | Enterprise |
|---|:---:|:---:|
| Tracking plans, scans, monitoring, alerts | ✓ | ✓ |
| Organizations, roles, groups, API keys | ✓ | ✓ |
| Audit log | ✓ | ✓ |
| Sign in with Google | ✓ | ✓ |
| Single sign-on per organization (OpenID Connect, SAML 2.0), verified domains, SSO required | | ✓ |
| Provisioning over SCIM 2.0, groups mapped to roles | | ✓ |
| Audit webhook to a SIEM | | ✓ |

Single sign-on per organization and SCIM provisioning are in the Enterprise
image only. The audit webhook is moving out of the Community code; until it
has, it still works in Community.

A Community instance shows each Enterprise feature in Settings with an
**Enterprise** tag. Its page says what the feature does.

To install the Enterprise image, see `tripl install --edition enterprise` in
the [CLI reference](./run/cli.md). To get access, write to sales@tripl.io.
