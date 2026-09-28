# ruff: noqa: F811  (the fixtures imported from test_org_sso / test_scim are redefined)
"""SCIM 2.0 ``/Groups`` and the admin-group mapping (F20, GH #273).

* groups are organization groups: create (managed by SCIM), read, filter by
  ``displayName``, replace, delete; PATCH adds, removes (Azure AD's
  ``members[value eq "<id>"]`` path included) and replaces members;
* a SCIM-managed group refuses manual renames, deletes and member changes
  through the groups API (409), its description stays editable;
* the owner maps a group to organization role ``admin``: joining it promotes a
  member, leaving it demotes an admin, an owner keeps ``owner``; the mapping
  is an owner's (admin 403);
* every write is audited with no user and the token's prefix.
"""

from __future__ import annotations

import uuid
from typing import Any

from tripl.tests.test_org_sso import (  # noqa: F401 - fixtures
    ACME,
    API,
    People,
    _audit,
    acme,
    people,
)
from tripl.tests.test_scim import (  # noqa: F401 - fixtures
    assert_scim_error,
    org_role,
    patch,
    scim,
    token,
)

GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
GROUPS_URL = f"{API}/orgs/{ACME}/groups"
CONFIG_URL = f"{API}/orgs/{ACME}/scim/config"


def group_body(name: str, *members: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "schemas": [GROUP_SCHEMA],
        "displayName": name,
        "members": [{"value": str(m)} for m in members],
        **extra,
    }


async def create_group(token: str, name: str, *members: uuid.UUID) -> dict[str, Any]:
    resp = await scim(token, "POST", "/Groups", group_body(name, *members))
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


def member_ids(resource: dict[str, Any]) -> set[str]:
    return {m["value"] for m in resource.get("members", [])}


async def test_group_crud(people: People, acme: uuid.UUID, token: str) -> None:
    mia, bob = people.ids["mia"], people.ids["bob"]
    created = await scim(
        token, "POST", "/Groups", group_body("Engineering", mia, externalId="okta-eng")
    )
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["displayName"] == "Engineering"
    assert group["externalId"] == "okta-eng"
    assert member_ids(group) == {str(mia)}
    assert created.headers["location"].endswith(f"/Groups/{group['id']}")

    listed = await people["root"].get(GROUPS_URL)
    [row] = listed.json()
    assert row["id"] == group["id"]
    assert row["managed_by_scim"] is True
    assert row["member_count"] == 1

    read = await scim(token, "GET", f"/Groups/{group['id']}")
    assert read.status_code == 200 and read.json()["displayName"] == "Engineering"
    lean = await scim(
        token, "GET", f"/Groups/{group['id']}", params={"excludedAttributes": "members"}
    )
    assert "members" not in lean.json()
    found = await scim(token, "GET", "/Groups", params={"filter": 'displayName eq "engineering"'})
    assert [r["id"] for r in found.json()["Resources"]] == [group["id"]]

    replaced = await scim(token, "PUT", f"/Groups/{group['id']}", group_body("Platform", bob))
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["displayName"] == "Platform"
    assert member_ids(replaced.json()) == {str(bob)}
    assert "externalId" not in replaced.json()

    duplicate = await scim(token, "POST", "/Groups", group_body("platform"))
    assert_scim_error(duplicate, 409, "uniqueness")

    deleted = await scim(token, "DELETE", f"/Groups/{group['id']}")
    assert deleted.status_code == 204, deleted.text
    assert_scim_error(await scim(token, "GET", f"/Groups/{group['id']}"), 404)
    assert (await people["root"].get(GROUPS_URL)).json() == []

    for action in ("org.scim.group_create", "org.scim.group_update", "org.scim.group_delete"):
        [row_] = await _audit(action)
        assert row_.user_id is None
        assert row_.organization_id == acme
        assert row_.payload["via"] == "scim"


async def test_patch_members_add_remove_and_azure_filter_path(people: People, token: str) -> None:
    mia, bob, root = people.ids["mia"], people.ids["bob"], people.ids["root"]
    group = await create_group(token, "Analysts")
    url = f"/Groups/{group['id']}"

    added = await scim(
        token,
        "PATCH",
        url,
        patch(
            {"op": "add", "path": "members", "value": [{"value": str(mia)}, {"value": str(bob)}]}
        ),
    )
    assert added.status_code == 200, added.text
    assert member_ids(added.json()) == {str(mia), str(bob)}

    # Azure AD removes one member through a filtered path.
    removed = await scim(
        token,
        "PATCH",
        url,
        patch({"op": "Remove", "path": f'members[value eq "{mia}"]'}),
    )
    assert removed.status_code == 200, removed.text
    assert member_ids(removed.json()) == {str(bob)}

    # Okta: remove with a value list, then a path-less replace of the name.
    okta = await scim(
        token,
        "PATCH",
        url,
        patch(
            {"op": "remove", "path": "members", "value": [{"value": str(bob)}]},
            {"op": "replace", "value": {"id": group["id"], "displayName": "Data"}},
        ),
    )
    assert okta.status_code == 200, okta.text
    assert member_ids(okta.json()) == set()
    assert okta.json()["displayName"] == "Data"

    replaced = await scim(
        token,
        "PATCH",
        url,
        patch({"op": "replace", "path": "members", "value": [{"value": str(root)}]}),
    )
    assert member_ids(replaced.json()) == {str(root)}

    stranger = people.ids["stranger"]
    unknown = await scim(
        token,
        "PATCH",
        url,
        patch({"op": "add", "path": "members", "value": [{"value": str(stranger)}]}),
    )
    assert_scim_error(unknown, 400, "invalidValue")


async def test_a_managed_group_refuses_manual_edits(people: People, token: str) -> None:
    root = people["root"]
    group = await create_group(token, "Managed", people.ids["mia"])
    url = f"{GROUPS_URL}/{group['id']}"

    add = await root.post(f"{url}/members", json={"user_id": str(people.ids["bob"])})
    assert add.status_code == 409, add.text
    remove = await root.delete(f"{url}/members/{people.ids['mia']}")
    assert remove.status_code == 409, remove.text
    rename = await root.patch(url, json={"name": "Renamed"})
    assert rename.status_code == 409, rename.text
    assert (await root.delete(url)).status_code == 409

    described = await root.patch(url, json={"description": "Synced from the IdP"})
    assert described.status_code == 200, described.text
    assert described.json()["managed_by_scim"] is True

    # A manual group stays manual.
    manual = await root.post(GROUPS_URL, json={"name": "Manual"})
    assert manual.status_code == 201, manual.text
    assert manual.json()["managed_by_scim"] is False
    ok = await root.post(
        f"{GROUPS_URL}/{manual.json()['id']}/members", json={"user_id": str(people.ids["bob"])}
    )
    assert ok.status_code == 201, ok.text


async def test_the_admin_group_mapping_promotes_and_demotes(
    people: People, acme: uuid.UUID, token: str
) -> None:
    root = people["root"]
    mia, root_id = people.ids["mia"], people.ids["root"]
    group = await create_group(token, "tripl-admins")

    config = await root.get(CONFIG_URL)
    assert config.status_code == 200, config.text
    assert config.json()["base_url"].endswith(f"/scim/v2/{ACME}")
    assert config.json()["admin_group_id"] is None
    assert config.json()["active_tokens"] == 1

    for name in ("bob", "mia"):
        refused = await people[name].put(CONFIG_URL, json={"admin_group_id": group["id"]})
        assert refused.status_code == 403, refused.text
    missing = await root.put(CONFIG_URL, json={"admin_group_id": str(uuid.uuid4())})
    assert missing.status_code == 404, missing.text

    mapped = await root.put(CONFIG_URL, json={"admin_group_id": group["id"]})
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["admin_group_name"] == "tripl-admins"

    url = f"/Groups/{group['id']}"
    add = patch(
        {"op": "add", "path": "members", "value": [{"value": str(mia)}, {"value": str(root_id)}]}
    )
    assert (await scim(token, "PATCH", url, add)).status_code == 200
    assert await org_role(acme, mia) == "admin"
    assert await org_role(acme, root_id) == "owner"

    drop = patch({"op": "remove", "path": f'members[value eq "{mia}"]'})
    assert (await scim(token, "PATCH", url, drop)).status_code == 200
    assert await org_role(acme, mia) == "member"
    assert await org_role(acme, root_id) == "owner"

    role_rows = await _audit("org.member_role_update")
    assert [(r.payload["old_role"], r.payload["new_role"]) for r in role_rows] == [
        ("member", "admin"),
        ("admin", "member"),
    ]
    assert all(r.payload["via"] == "scim_admin_group" and r.user_id is None for r in role_rows)
    [config_row] = await _audit("org.scim.config_update")
    assert config_row.user_id == people.ids["root"]


async def test_mapping_an_existing_group_applies_at_once(
    people: People, acme: uuid.UUID, token: str
) -> None:
    root = people["root"]
    mia = people.ids["mia"]
    group = await create_group(token, "Leads", mia)
    assert await org_role(acme, mia) == "member"

    await root.put(CONFIG_URL, json={"admin_group_id": group["id"]})
    assert await org_role(acme, mia) == "admin"
    await root.put(CONFIG_URL, json={"admin_group_id": None})
    assert await org_role(acme, mia) == "member"

    # Deleting the mapped group demotes its members and clears the mapping.
    await root.put(CONFIG_URL, json={"admin_group_id": group["id"]})
    assert await org_role(acme, mia) == "admin"
    assert (await scim(token, "DELETE", f"/Groups/{group['id']}")).status_code == 204
    assert await org_role(acme, mia) == "member"
    assert (await root.get(CONFIG_URL)).json()["admin_group_id"] is None


async def test_groups_of_another_org_are_invisible(people: People, token: str) -> None:
    root = people["root"]
    other = await root.post(f"{API}/orgs", json={"slug": "globex", "name": "Globex"})
    assert other.status_code == 201, other.text
    theirs = await root.post(f"{API}/orgs/globex/groups", json={"name": "Globex team"})
    assert theirs.status_code == 201, theirs.text
    group_id = theirs.json()["id"]

    assert_scim_error(await scim(token, "GET", f"/Groups/{group_id}"), 404)
    assert_scim_error(await scim(token, "DELETE", f"/Groups/{group_id}"), 404)
    listing = (await scim(token, "GET", "/Groups")).json()
    assert group_id not in {r["id"] for r in listing["Resources"]}
