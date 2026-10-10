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
