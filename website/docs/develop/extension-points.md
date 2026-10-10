---
title: Extension points
---

# Extension points

A separately installed Python package can add to the tripl server without
patching it. The server finds such packages through entry points and calls them
at fixed points: the **hooks** in `backend/src/tripl/extensions.py`. With no
extension installed, every hook is a no-op and the server behaves as plain
Community tripl.

## Registering an extension

An extension is an instance of `tripl.extensions.Extension`. A package declares
it in the `tripl.extensions` entry point group:

```toml
[project.entry-points."tripl.extensions"]
my-extension = "my_package.extension:extension"
```

Its ORM model modules go in a group of their own, `tripl.models`, whose values
are module paths:

```toml
[project.entry-points."tripl.models"]
my-extension = "my_package.models"
```

The separate group is needed because `tripl.models` imports these modules at
the end of its own initialisation, before any service can be imported. The
extension object cannot be reached that early.

Extensions are loaded once per process and called in load order, sorted by
entry point name. A server that
finds an entry point whose object is not an `Extension` refuses to start.

## Database migrations

An extension that owns tables keeps its own Alembic history, with its own
version table, so the core keeps a single migration head. To run it, the
extension declares a callable taking no arguments in the `tripl.migrations`
entry point group:

```toml
[project.entry-points."tripl.migrations"]
my-extension = "my_package.migrations:upgrade"
```

The `migrate` one-shot of `compose.yaml` runs `python -m tripl.migrate`. That
command runs the core's `alembic upgrade head` first, then each extension's
callable, sorted by entry point name.

## Hooks

Override only the hooks you need; the base class implements each one as a
no-op.

| Hook | Called when | What it may do |
|---|---|---|
| `api_routers()` | the API router is built | Return `ExtensionRouter`s to mount under `/api/v1`, ahead of the core's routes. `protected=True` adds the API's authentication dependencies; `outbound` names what a write would send out of the instance, so the public demo refuses it. |
| `install_app(app)` | the app is built | Mount routers outside `/api/v1` and register exception handlers. |
| `error_response(path, kind, status, detail, headers)` | an HTTP error, a validation error or a too-large body is about to be answered | Return a response in the extension's own error format for the paths it owns, or `None`. The first non-`None` answer wins. |
| `org_session_gate(request, session, user, org, role=)` | a browser session acts in an organization | Raise `GateRefused` to refuse it. |
| `api_key_use_gate(request, session, user, org_id, api_key)` | an API key authenticates | Raise `GateRefused` to refuse it. |
| `api_key_mint_gate(session, org)` | a browser session creates an API key | Raise `GateRefused` to refuse it. |
| `project_grants()` | any project-access answer is computed (`tripl.services.project_access`): a project's detail, the project list, the membership and write gates, notification and alert fan-outs | Return a SQL `SELECT` of four columns, in order: organization id, project id, user id, role (`editor` or `viewer`), or `None`. A row counts only when its organization is the project's own and the user is a member of that organization; any other role, `owner` included, counts for nothing. The higher of the grant and the user's own project role wins. A grant never changes an organization role. Called while a query is built: no I/O. |
| `project_permission_check(session, user, project_id, permission)` | an editor (not an owner or admin of the project's organization) writes in a project | Return `False` to refuse the write with `403`; `True` or `None` leave it to the core, so the hook can take a permission away but never grant one. `permission` is one of `tripl.services.project_permissions.PERMISSIONS` (`plan.edit`, `plan.merge`, `comments.write`, `docs.edit`, `metrics.manage`, `alerts.manage`, `data_sources.manage`, `settings.manage`), mapped from the route; an unmapped route asks for `settings.manage`. An exception fails the request. |
| `on_member_removed(session, org_id, user_id, by_admin=)` | a member leaves an organization or is removed | Clean up the extension's rows. Return counts by name; they are added to the removal's audit entry. |
| `on_owner_demoted(session, org_id, user_id)` | an owner stops being an owner | Revoke owner-only credentials. |
| `on_membership_restored(session, org_id, user_id)` | a removed member joins again through an invitation | Lift blocks set on removal. |
| `on_org_deleting(session, org_id)` | an organization is being deleted, before its groups | Delete the extension's rows for it. |
| `on_group_change(session, org_id, group_id, added=, removed=)` | members are added to or removed from an organization group | React to the change, for example by mapping groups to roles. |
| `on_audit_recorded(session, entry, org_id)` | an organization's audit row is added | Forward it, in the writing transaction. |
| `claim_ready_demo(session, visitor_id=, organization_id=, name=)` | a signed-in visitor starts a demo project (`demo_service.create_demo_project`) | Return a demo project seeded ahead of time, moved to the visitor in that organization and renamed to `name`, or `None` to seed one now as Community does. Runs in the caller's transaction and must not commit. The first extension returning a project wins. |
| `on_ready_demo_claimed()` | after a demo from `claim_ready_demo` has been handed out and committed | Called on the extension that supplied it only, for example to refill a pool of ready demos. |
| `tenancy()` | a decision differs between one team's instance and a multi-tenant service | Return a `tripl.tenancy.TenancyPolicy` to run the instance as a multi-tenant service, or `None`. The first policy returned wins; without one the instance is a single team's, and `DEPLOYMENT_MODE=hosted` refuses to start. |
| `secret_cipher()` | the first stored secret is encrypted or decrypted, or the API or worker starts | Return a `tripl.crypto.SecretCipher` (`encrypt`, `decrypt`, `check`) to encrypt every stored secret, or `None`. The first cipher returned wins; without one, secrets are Fernet under `ENCRYPTION_KEY`. `decrypt` raises `InvalidToken` for a value it cannot read; `check` runs when the API and the worker start and refuses startup when it raises. |
| `stored_secret_slots()` | stored secrets are listed (`tripl.services.stored_secrets`) | Return the `ColumnSecret`s and `JsonSecret`s the extension's own tables hold, so re-encrypting every stored secret reaches them. A `*_encrypted` column no slot lists fails Community's tests. |
| `plan_policy_violations(session, context)` | `POST /plan/validate` (`context.phase == "validate"`), a branch merge after the project's own gates (`"merge"`), and a write to the main plan (`"direct_edit"`) | Return `tripl.core.plan_policy.PolicyViolation`s. In `validate`, each one names its call by `item_ref` and is reported as a `policy_violation` finding (one naming no call is dropped). In `merge` and `direct_edit`, any `error` violation refuses the request with `409` and `policy_violations`; `warning` ones never block. The context carries the calls, the merge base and branch snapshots with who approved the current content, or just the project and actor. Read-only. |
| `celery_task_modules()` | the Celery app is configured | Return modules to import so their tasks register. |
| `beat_schedule()` | the Celery app is configured | Return beat entries to add. |

The hooks that take a session run inside the caller's transaction. They must
not commit: what they write commits or rolls back with the core's change.

## Refusing a request

`GateRefused` is an `HTTPException` with an `extra` mapping. The server answers
it as `{"detail": ..., **extra}` with the exception's status (403 by default),
so a gate can tell the client how to get through:

```python
from tripl.extensions import Extension, GateRefused


class RequireSso(Extension):
    name = "require-sso"

    async def org_session_gate(self, request, session, user, org, *, role=None):
        if not signed_in_through_sso(request, org):
            raise GateRefused(
                "This organization requires single sign-on",
                extra={"sso_start": sso_login_path(org.slug)},
            )
```

## Testing

`tripl.extensions.override_extensions([...])` runs a block with exactly the
given extensions installed. `override_extensions([])` checks the behavior of a
server that has no extensions. Routers are mounted when the app is built, so an
override changes hook dispatch but not the set of routes.
`backend/src/tripl/tests/test_extensions.py` has examples.

The route audits (cross-organization isolation, the write gate, the owner and
API-key gates) walk only the routes Community declares, with
`tripl.tests.test_rbac.iter_api_routes()`, so they pass with an extension
installed; the OpenAPI snapshot is skipped then, since it is Community's API.
An extension audits its own routes: `iter_api_routes("<its package>")` lists
the routes whose handlers live in that package.

## Frontend extensions

The web app has a matching registry in `frontend/src/extensions`. A frontend
extension is a `FrontendExtension` object (`frontend/src/extensions/types.ts`);
the app renders what it lists without knowing what it is:

| Field | What the app does with it |
|---|---|
| `routes` | Mounts each as a top-level route outside the app shell, lazily loaded. `title` sets the browser-tab title; without one the tab reads just "tripl". `skeleton` is deprecated and ignored. |
| `settingsSections` | Adds a rail item to a Settings group and opens its page at the item's path. The item goes before the item named by `before`, else after the one named by `after`, else at the end of the group. `access` decides who may open it: `orgOwner` (organization owners only), `owner` (owners and admins) or `platform` (platform admins, whatever their organization role); anyone else sees a read-only notice, which for `orgOwner` gives `deniedReason` as what the page is and why it is an owner's. With `subpaths`, the page also answers the paths below its own (`platform/orgs/<slug>`) and gets the rest as its `subpath` prop. Set the item's `refusedOnPublicDemo` on the page of a router marked `outbound`: a public demo leaves it out of the rail and the palettes, and its address says the demo does not offer it. |
| `authPanels` | Offers a button under the password form on the sign-in page, which opens the panel. |
| `shellGates` | Called with the app shell's request errors; a gate returns a screen to show instead of the shell, or `null`. |
| `shellBanners` | Components rendered across the top of the app shell. Each renders nothing when it has nothing to say. |

Frontend extensions come from the `@tripl/extensions` module, a
build-time alias. By default it points to `src/extensions/none.ts`, which lists
none. A build that sets `TRIPL_EXTENSIONS_ENTRY` to the path of another module
gets that module's default export, a `FrontendExtension[]`, instead.

A refusal's fields beside `detail` (a `GateRefused`'s `extra`) reach the
frontend as `ApiError.extra`, so a shell gate can read them, for example
`sso_start`.

## Enterprise features in Community

When a feature moves to the Enterprise edition, Community keeps showing that
it exists. The feature does not just disappear from Community.

- **Settings.** `frontend/src/extensions/teasers.ts` lists each Enterprise
  feature as a teaser: its rail item tagged **Enterprise** and a one-line
  summary. Its page (`EnterpriseFeature`) says the feature is in Enterprise and
  links to the [Editions](../editions.md) page. A teaser has the id of the
  section the Enterprise extension registers, and it is hidden whenever an
  installed extension provides that id. An Enterprise build therefore shows
  the real page. A teaser may carry `inCommunity: { text, href }`: what
  Community already has of the same kind, which its page shows with a "How to
  set it up" link, so someone who searched for the Enterprise feature still
  finds the Community one.
- **One definition per feature.** `enterpriseTeaserSection(id)` returns a
  teaser's `group`, `after`, `before` and `item` without the **Enterprise** tag.
  The extension that provides the real page builds its section from it and adds
  only the page, `access` and what the page needs, so both editions share one
  label, icon, path, placement and keyword list. It throws on an id no teaser
  has.
- **Docs.** The [Editions](../editions.md) page lists every Enterprise
  feature. A page that documents one opens with this admonition:

  ```md
  :::info Enterprise
  This feature is part of the [Enterprise edition](../editions.md).
  :::
  ```

  Its full documentation lives in the **Enterprise** section,
  `website/docs/enterprise/`, whose pages open with the same admonition. Only
  what an operator of tripl's own services needs (hosted mode, the public demo,
  issuing license keys) stays in the private repository.

Moving a feature out adds its teaser (when it has a settings page), a row on
the Editions page, and the admonition on its docs pages.

## The Enterprise package

Community bundles no extension. Every Enterprise feature lives in the
separately installed, private Enterprise package; the
[Editions](../editions.md) page lists them.

Only the extension named `enterprise` (the Enterprise package's) makes an
instance report the edition `enterprise`. Any other extension leaves it a
Community instance.

Besides the hooks and the registry above, the Enterprise package imports
Community modules directly: models, schemas, services,
`tripl.middleware.rate_limit`, `tripl.worker.db` and the alert channel
helpers in `tripl/worker/tasks`. It pins a Community commit in its
`COMMUNITY_REF` file, and its nightly CI runs against Community `main`.
`backend/src/tripl/tests/test_extension_api_surface.py` lists the Community
names it relies on, so renaming one fails Community's own CI: keep the old
name as an alias or change Enterprise in step.

Two of those helpers are public and meant for any extension:

- `tripl.middleware.rate_limit`: `limiter_for` builds a token-bucket limiter,
  `enforce(limiter)` is a route dependency that answers `429`, and `allow` and
  `retry_after_for_key` check a limiter by hand, for an extension's own routes.
- `tripl.worker.tasks.alerts_plain.send_plain_message` sends a plain message
  through any alert destination (Slack, Telegram, webhook, email, Jira, Linear,
  PagerDuty or Microsoft Teams).
