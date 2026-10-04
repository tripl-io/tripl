"""Regression coverage for search-document storage limits."""

import uuid

from tripl.models.search_document import SearchDocument
from tripl.services._search_documents import BuiltDocument


def test_built_document_caps_title_and_subtitle_to_storage_width() -> None:
    title_width = SearchDocument.__table__.c.title.type.length
    subtitle_width = SearchDocument.__table__.c.subtitle.type.length
    document = BuiltDocument(
        entity_type="event",
        entity_id=uuid.uuid4(),
        parent_event_id=None,
        title="t" * (title_width + 1),
        subtitle="s" * (subtitle_width + 1),
        body="body",
        keywords="keywords",
        route_path="/p/example/events/page-view",
    )

    assert document.title == "t" * title_width
    assert document.subtitle == "s" * subtitle_width
