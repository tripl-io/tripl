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

Extensions are loaded once per process and called in load order: the bundled
one first, then the installed ones sorted by entry point name. A server that
finds an entry point whose object is not an `Extension` refuses to start.

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
| `on_member_removed(session, org_id, user_id, by_admin=)` | a member leaves an organization or is removed | Clean up the extension's rows. Return counts by name; they are added to the removal's audit entry. |
| `on_owner_demoted(session, org_id, user_id)` | an owner stops being an owner | Revoke owner-only credentials. |
| `on_membership_restored(session, org_id, user_id)` | a removed member joins again through an invitation | Lift blocks set on removal. |
| `on_org_deleting(session, org_id)` | an organization is being deleted, before its groups | Delete the extension's rows for it. |
| `on_group_change(session, org_id, group_id, added=, removed=)` | members are added to or removed from an organization group | React to the change, for example by mapping groups to roles. |
| `on_audit_recorded(session, entry, org_id)` | an organization's audit row is added | Forward it, in the writing transaction. |
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

## Frontend extensions

The web app has a matching registry in `frontend/src/extensions`. A frontend
extension is a `FrontendExtension` object (`frontend/src/extensions/types.ts`);
the app renders what it lists without knowing what it is:

| Field | What the app does with it |
|---|---|
| `routes` | Mounts each as a top-level route outside the app shell, lazily loaded. |
| `settingsSections` | Adds a rail item to a Settings group (after the item named by `after`) and opens its page at the item's path. `access` decides who may open it: `orgOwner` (owners only) or `owner` (owners and admins). |
| `authPanels` | Offers a button under the password form on the sign-in page, which opens the panel. |
| `shellGates` | Called with the app shell's request errors; a gate returns a screen to show instead of the shell, or `null`. |

Extensions beyond the bundled one come from the `@tripl/extensions` module, a
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
  the real page, and so does Community while the feature is still bundled.
- **Docs.** The [Editions](../editions.md) page lists every Enterprise
  feature. A page that documents one opens with this admonition:

  ```md
  :::info Enterprise
  This feature is part of the [Enterprise edition](../editions.md).
  :::
  ```

Moving a feature out adds its teaser (when it has a settings page), a row on
the Editions page, and the admonition on its docs pages.

## The bundled extension

Single sign-on, SCIM provisioning and the audit webhook still live in this
repository. They are registered as a bundled extension: on the backend
`tripl._bundled_enterprise`, with its models in the bundled model list; in the
web app `frontend/src/extensions/bundled`. The core reaches them only through
the hooks and the registry above. When they move to a separately installed
package, only those modules and their imports move with them.
