# Security policy

## Reporting a vulnerability

Please do not report security problems in public issues, discussions, or pull
requests.

Report them privately through GitHub instead: go to the repository's
**Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/tripl-io/tripl/security/advisories/new)).
The report is visible only to the maintainers.

If you cannot use GitHub, write to security@tripl.io.

Include in the report:

- the version, which is the image tag or the commit;
- how Tripl is deployed;
- the steps to reproduce the problem;
- what an attacker gains.

## What happens next

- We acknowledge the report within 3 working days.
- We send a first assessment within 10 working days.
- We agree a disclosure date with you. The default is 90 days after the report,
  or sooner once a fixed release is out.
- With your permission, we credit you in the advisory.

## Supported versions

Security fixes go into the latest release. Upgrade with `tripl upgrade`, or
pull the newest image.

## Scope

In scope: the server, the worker, the web app, the CLI, and the MCP server in
this repository.

Out of scope:

- findings that need the operator's own credentials or a compromised host;
- missing hardening headers that have no demonstrated impact;
- volumetric denial of service.

[Security & secrets](https://docs.tripl.io/run/security) describes
how a deployment is meant to be hardened.
