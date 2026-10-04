"""Search hits folded into variant groups.

Events of one event type whose names differ only in the value substituted for
one naming-rule placeholder are one group: the best-ranked member stays in the
list and carries ``variant_group``; the others leave the page and are listed
inside the group so the UI can expand them.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tripl.schemas.search import SearchResult
from tripl.services.search_service import group_event_variants


def _event(title: str, score: float) -> SearchResult:
    event_id = uuid.uuid4()
    return SearchResult(
        id=uuid.uuid4(),
        entity_type="event",
        entity_id=event_id,
        parent_event_id=event_id,
        title=title,
        route_path=f"/p/demo/events/detail/{event_id}",
        score=score,
    )


def _other(entity_type: str, title: str, score: float) -> SearchResult:
    return SearchResult(
        id=uuid.uuid4(),
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=uuid.uuid4(),
        title=title,
        route_path="/",
        score=score,
    )


def _types(items: list[SearchResult], event_type_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
    return {
        item.parent_event_id: event_type_id for item in items if item.parent_event_id is not None
    }


def test_default_names_differing_in_one_segment_fold_under_the_best_ranked() -> None:
    event_type_id = uuid.uuid4()
    home = _event("event_name=Screen View | screen=Home", 3.0)
    settings = _event("event_name=Screen View | screen=Settings", 2.0)
    profile = _event("event_name=Screen View | screen=Profile", 1.0)
    ranked = [home, settings, profile]

    grouped = group_event_variants(
        ranked, event_types=_types(ranked, event_type_id), name_formats=[]
    )

    assert [item.id for item in grouped] == [home.id]
    group = grouped[0].variant_group
    assert group is not None
    assert group.count == 3
    assert group.placeholder == "screen"
    assert group.pattern == "event_name=Screen View | screen={screen}"
    assert group.key == f"{event_type_id}:event_name=Screen View | screen={{screen}}"
    # Best-ranked first, and every member is listed with the value it substituted.
    assert [(v.id, v.value) for v in group.variants] == [
        (settings.id, "Settings"),
        (profile.id, "Profile"),
    ]
    assert group.variants[0].event_id == settings.parent_event_id
    assert group.variants[0].route_path == settings.route_path


def test_the_input_list_is_not_mutated() -> None:
    event_type_id = uuid.uuid4()
    ranked = [
        _event("screen=Home | action=tap", 2.0),
        _event("screen=Home | action=swipe", 1.0),
    ]

    grouped = group_event_variants(
        ranked, event_types=_types(ranked, event_type_id), name_formats=[]
    )

    assert grouped[0].variant_group is not None
    assert ranked[0].variant_group is None
    assert len(ranked) == 2


def test_names_differing_in_two_placeholders_are_not_variants() -> None:
    event_type_id = uuid.uuid4()
    ranked = [
        _event("screen=Home | action=tap", 2.0),
        _event("screen=Settings | action=swipe", 1.0),
    ]

    grouped = group_event_variants(
        ranked, event_types=_types(ranked, event_type_id), name_formats=[]
    )

    assert [item.id for item in grouped] == [item.id for item in ranked]
    assert all(item.variant_group is None for item in grouped)


def test_events_of_different_types_never_fold_together() -> None:
    first = _event("screen=Home", 2.0)
    second = _event("screen=Settings", 1.0)
    event_types = {**_types([first], uuid.uuid4()), **_types([second], uuid.uuid4())}

    grouped = group_event_variants([first, second], event_types=event_types, name_formats=[])

    assert [item.variant_group for item in grouped] == [None, None]


def test_hand_written_names_are_not_grouped_with_rule_names() -> None:
    event_type_id = uuid.uuid4()
    ranked = [
        _event("Home Screen View", 3.0),
        _event("event_name=Home Screen View | screen=Home", 2.0),
        _event("event_name=Home Screen View | screen=Map", 1.0),
    ]

    grouped = group_event_variants(
        ranked, event_types=_types(ranked, event_type_id), name_formats=[]
    )

    assert [item.title for item in grouped] == [
        "Home Screen View",
        "event_name=Home Screen View | screen=Home",
    ]
    assert grouped[0].variant_group is None
    assert grouped[1].variant_group is not None
    assert grouped[1].variant_group.count == 2


def test_scan_name_format_is_read_back() -> None:
    event_type_id = uuid.uuid4()
    ranked = [
        _event("Screen View: Home (ios)", 3.0),
        _event("Screen View: Map (ios)", 2.0),
        _event("Screen View: Home (android)", 1.0),
    ]

    grouped = group_event_variants(
        ranked,
        event_types=_types(ranked, event_type_id),
        name_formats=["Screen View: {screen} ({platform})"],
    )

    # "Home (ios)" could vary on either slot, each gathering two members; the
    # first one found wins and the leftover row stays alone in its rank place.
    assert len(grouped) == 2
    group = grouped[0].variant_group
    assert group is not None
    assert group.count == 2
    assert group.placeholder in {"screen", "platform"}
    assert "{" + group.placeholder + "}" in group.pattern
    assert grouped[1].variant_group is None


def test_the_placeholder_gathering_most_members_wins() -> None:
    event_type_id = uuid.uuid4()
    ranked = [
        _event("screen=Home | platform=ios", 4.0),
        _event("screen=Home | platform=android", 3.0),
        _event("screen=Map | platform=ios", 2.0),
        _event("screen=Cart | platform=ios", 1.0),
    ]

    grouped = group_event_variants(
        ranked, event_types=_types(ranked, event_type_id), name_formats=[]
    )

    assert [item.title for item in grouped] == [
        "screen=Home | platform=ios",
        "screen=Home | platform=android",
    ]
    group = grouped[0].variant_group
    assert group is not None
    assert group.placeholder == "screen"
    assert [v.value for v in group.variants] == ["Map", "Cart"]


def test_ambiguous_formats_are_not_used_to_group() -> None:
    event_type_id = uuid.uuid4()
    ranked = [_event("Home", 2.0), _event("Map", 1.0)]

    # A bare placeholder would fold every event of the type into one row, and
    # two adjacent placeholders cannot be split back apart.
    for name_format in ("{screen}", "{screen}{platform}", "{screen}-{screen}"):
        grouped = group_event_variants(
            ranked,
            event_types=_types(ranked, event_type_id),
            name_formats=[name_format],
        )
        assert [item.variant_group for item in grouped] == [None, None]


def test_non_event_hits_keep_their_rank_place() -> None:
    event_type_id = uuid.uuid4()
    event_type_hit = _other("event_type", "Screen View", 5.0)
    home = _event("screen=Home", 4.0)
    variable = _other("variable", "screen", 3.0)
    map_ = _event("screen=Map", 2.0)
    ranked = [event_type_hit, home, variable, map_]

    grouped = group_event_variants(
        ranked, event_types=_types(ranked, event_type_id), name_formats=[]
    )

    assert [item.id for item in grouped] == [event_type_hit.id, home.id, variable.id]
    assert grouped[1].variant_group is not None
    assert [v.id for v in grouped[1].variant_group.variants] == [map_.id]


async def _seed(client: AsyncClient, slug: str) -> tuple[str, str]:
    await client.post("/api/v1/projects", json={"name": "Variants", "slug": slug})
    first = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "screen_view", "display_name": "Screen view"},
    )
    second = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "click", "display_name": "Click"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    return first.json()["id"], second.json()["id"]


@pytest.mark.asyncio
async def test_search_endpoint_folds_variants_only_when_asked(client: AsyncClient) -> None:
    slug = "search-variants"
    screen_type, click_type = await _seed(client, slug)
    for event_type_id, name in (
        (screen_type, "zvariant=yes | screen=Home"),
        (screen_type, "zvariant=yes | screen=Map"),
        (screen_type, "zvariant=yes | screen=Cart"),
        (click_type, "zvariant=yes | screen=Home"),
    ):
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type_id, "name": name},
        )
        assert created.status_code == 201

    plain = await client.get(f"/api/v1/projects/{slug}/search?q=zvariant&types=event")
    assert plain.status_code == 200
    plain_items = plain.json()["items"]
    assert len(plain_items) == 4
    assert all(item["variant_group"] is None for item in plain_items)

    grouped = await client.get(
        f"/api/v1/projects/{slug}/search?q=zvariant&types=event&group_variants=true"
    )
    assert grouped.status_code == 200
    body = grouped.json()
    # The three screen_view events are one row; the click event stays apart.
    assert body["total"] == 2
    assert len(body["items"]) == 2
    groups = [item["variant_group"] for item in body["items"] if item["variant_group"]]
    assert len(groups) == 1
    assert groups[0]["count"] == 3
    assert groups[0]["placeholder"] == "screen"
    assert len(groups[0]["variants"]) == 2
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_grouped_rows_page_by_row_not_by_member(client: AsyncClient) -> None:
    slug = "search-variants-page"
    screen_type, click_type = await _seed(client, slug)
    for event_type_id, name in (
        (screen_type, "zpage=yes | screen=Home"),
        (screen_type, "zpage=yes | screen=Map"),
        (screen_type, "zpage=yes | screen=Cart"),
        (click_type, "zpage=yes | button=Buy"),
    ):
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type_id, "name": name},
        )
        assert created.status_code == 201

    # Two rows exist after folding, so a two-row page holds them all and is not
    # truncated, even though four events matched.
    response = await client.get(
        f"/api/v1/projects/{slug}/search?q=zpage&types=event&group_variants=true&limit=2"
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["truncated"] is False

    one_row = await client.get(
        f"/api/v1/projects/{slug}/search?q=zpage&types=event&group_variants=true&limit=1"
    )
    assert one_row.json()["truncated"] is True
