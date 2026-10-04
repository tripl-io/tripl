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

## The bundled extension

Single sign-on, SCIM provisioning and the audit webhook still live in this
repository. They are registered as a bundled extension,
`tripl._bundled_enterprise`, and their models are in the bundled model list.
The core reaches them only through the hooks above. When they move to a
separately installed package, only that module and its imports move with them.
