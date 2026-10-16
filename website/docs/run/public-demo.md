---
title: Running a public demo
sidebar_position: 8
---

# Running a public demo

:::info Enterprise
A public demo is part of the [Enterprise edition](../editions.md): the
`PUBLIC_DEMO` switch, the pool of demos seeded ahead of time and the nightly
purge of idle organizations are its package's, and a public demo runs hosted.
Its recipe is documented with that edition.
:::

A public demo is a tripl instance anyone can sign in to with Google and try on
generated demo projects. Every visitor gets an organization of their own, and
whatever would reach outside the instance is refused: connecting a warehouse,
delivering alerts outside the app, filing tracker tickets, AI.

## The demo warehouse and the pool

Every demo on the instance reads the same [demo
warehouse](../use/demo-workspace.md#the-demo-warehouse), so a public demo
prepares no data per visitor: a demo's plan is written and its history collected
from the warehouse when it is created. What grows with visitors is tripl's own
database, and the daily prune keeps each demo's observed rows inside the
warehouse's retention.

Building a demo takes about half a minute of worker time before it opens, and
a few more minutes after, filling in the older history while the visitor
looks around (one worker task each, so a busy instance queues them rather than
stalling). Keep a small pool seeded
ahead of time, so a visitor gets one at once: `DEMO_POOL_SIZE=2` or `3` covers
visitors arriving minutes apart, and the pool refills after each claim. A larger
pool mostly spends worker time seeding demos nobody claims before
`DEMO_POOL_MAX_AGE_HOURS` discards them. Run the warehouse on PostgreSQL rather
than the in-memory engine (**Settings → Instance → System** says which one demos
use): the in-memory engine regenerates rows in each worker process, which a busy
instance pays for on every collection.

## Semantic search

Demo projects search by meaning out of the box, from embedding vectors bundled
with the release for the demo's own text and its suggested queries (`purchase
funnel`, `money back`); other queries fall back to keyword matching. For semantic search on any query, the operator sets
`PUBLIC_DEMO_SEARCH_EMBEDDINGS=true` alongside `SEARCH_EMBEDDINGS_ENABLED=true`
and an embeddings key: a public demo otherwise drops that key with every other
AI credential. What visitors type into search, and the demo projects' text,
then go to the embedding provider on the operator's key, so use a key of its
own with a budget cap set at the provider. Generative AI stays off either way.

## Signing in

The sign-in page of a public demo offers **Continue with Google** and nothing
else: no email and password form, no single sign-on panels, no sign-up tabs.
The operator's own accounts still have passwords; they sign in at
`/auth?mode=password`, which shows the full form. The address is not linked from
anywhere in the app.

## Share a demo with colleagues

On a public demo an invitation is a link only (no email) and always grants
**Member**. The colleague signs in with Google using the invited address, then
opens the link and accepts it. They keep their own workspace and gain
**Viewer** access to the inviting organization's demo projects: those ready when
they accept, and each demo generated in that organization later. A project role
they already had is kept.

## Settings

Settings offers a visitor only what the demo takes. The organization's own
**Email**, **AI**, **Semantic search**, **Photos**, **Trackers** and **Limits** are left
out of the rail and both command palettes, as are, with the Enterprise edition,
**Single sign-on**, **Provisioning** and **Audit webhook**: the server refuses
every change to them on a public demo. Opened by its address, such a page says
the demo does not offer it and links to the quick start instead of showing a
form whose every save is refused.

Every edition can generate a demo project for a team to try tripl on
(**Generate demo project**); that is not a public demo.
