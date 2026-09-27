"""Word lists for event-name order rules (GH #265, F12).

Small, built-in and English: a word missing here only means a name does not
vote on verb order and is never reordered by the lint. Kept apart from
:mod:`name_similarity` so the lists can grow without crowding the logic.
"""

from __future__ import annotations

#: A small built-in English verb list for verb/noun order. Deliberately short:
#: a word missing here only means a name does not vote on the order.
VERBS: frozenset[str] = frozenset(
    {
        "open",
        "close",
        "view",
        "click",
        "tap",
        "press",
        "show",
        "hide",
        "start",
        "complete",
        "finish",
        "submit",
        "select",
        "swipe",
        "scroll",
        "play",
        "pause",
        "stop",
        "share",
        "search",
        "add",
        "remove",
        "delete",
        "create",
        "update",
        "edit",
        "save",
        "load",
        "login",
        "logout",
        "signup",
        "purchase",
        "buy",
        "subscribe",
        "unsubscribe",
        "cancel",
        "skip",
        "dismiss",
        "enter",
        "exit",
        "send",
        "receive",
        "download",
        "upload",
        "install",
        "launch",
        "visit",
        "watch",
        "like",
        "follow",
        "change",
        "toggle",
        "fail",
        "succeed",
        "confirm",
        "accept",
        "decline",
        "reject",
        "expand",
        "collapse",
        "refresh",
        "restore",
        "redeem",
        "rate",
        "invite",
    }
)

#: Verbs that are just as often nouns ("Purchase Completed", "Search Results",
#: "Share Sheet", "Rate Prompt", "Like Count"). They still vote when a
#: catalog's verb position is inferred, but the order rules never MOVE them:
#: a lint that turns "Purchase Done" into "Done Purchase" is worse than none.
AMBIGUOUS_VERBS: frozenset[str] = frozenset({"purchase", "search", "share", "rate", "like"})

#: Irregular participles. A name ending in one ("Signup Done", "Tutorial
#: Completed") describes a state, not an action with a movable verb.
IRREGULAR_PARTICIPLES: frozenset[str] = frozenset(
    {
        "done",
        "shown",
        "seen",
        "sent",
        "hidden",
        "begun",
        "given",
        "taken",
        "made",
        "gone",
        "paid",
        "bought",
        "sold",
        "won",
        "lost",
        "left",
        "held",
        "read",
    }
)
