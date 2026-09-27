---
title: Notifications & watching
sidebar_position: 10
---

# Notifications & watching

tripl tells you when something you care about changes: someone comments on an
event you work on, mentions you, asks an open question about your event, a
signal fires on a metric you watch, or a branch needs your review. Notifications
land in the **Notifications** tab of the bell in the top bar, and can also reach
you by email, one at a time or as a daily or weekly digest.

This page covers what you watch and why, what notifies you, how to quiet a
thread, how @mentions work and how email delivery is set. Alert rules are a
separate mechanism for routing signals to team channels; see
[Alerting rules](./alerting.md).

## Watching

You **watch** (subscribe to) individual things in a project:

| What | Where the Watch button is |
|------|---------------------------|
| An event | the event page's header, next to the event's name |
| An event type | the event type's page |
| A catalog metric | the metric's page |
| A plan branch | the branch's page |

Watching an event type brings the **signals**, **lifecycle findings** and
**open questions** on all of its events, so the owners of a type hear about
what matters on each of its events without watching every one. It does not
bring the ordinary comments and replies on those events: to follow an event's
discussion, watch the event itself (commenting on it does that for you).

**Watch** subscribes you by hand; **Unwatch** removes your subscription. Both
change only your own subscription and nobody else's.

### Automatic subscriptions

You do not have to watch everything by hand. tripl subscribes you when you
become involved:

| You become | You are subscribed to |
|------------|-----------------------|
| The **author** of an event (you created it) | that event |
| An **owner** of an event type | that event type (its events' signals, lifecycle findings and open questions) |
| A **commenter** (your first comment on an event) | that event |
| The **author** of a plan branch | that branch |
| A **reviewer** of a plan branch | that branch |

A subscription remembers every reason it exists (for example *author* and
*commenter*), plus *manual* when you pressed **Watch** yourself.

### Muting a thread

An event's comment thread carries a **Mute** toggle. Muting keeps your
subscription to the event and leaves the thread visible to you as before, but
the subscription stops notifying you. **@mentions still reach you** on a muted
event. While it is muted you still count as watching it: the Watch button in
the event's header reads **Muted** and offers **Unmute** (hear about it again)
and **Unwatch** (drop the subscription altogether). Muting is useful when an
event you authored has a long discussion you no longer need to follow, but you
do not want to lose the subscription tripl gave you as author.

## What notifies you

| Notification | Who receives it |
|--------------|-----------------|
| **New comment** or **reply** on an event | everyone watching the event (not the watchers of its event type) |
| **New comment** or **reply** on a plan branch | everyone watching the branch |
| **@mention** in any comment | the member you mentioned, whether or not they watch the event or branch, and even if they muted it |
| **Open question** on an event | the event's author and its event type's owners |
| **Signal** on an event, event type or metric | the people watching it; a signal on an event also reaches the watchers of its event type |
| **Lifecycle finding** opened on an event | the people watching the event or its event type |
| **Branch review requested** | the reviewers you asked |
| **Branch approved**, **branch merged** | the branch's author, its reviewers and everyone watching the branch |

A mention counts once: if a comment mentions you, you get the mention and not
also the plain comment or open-question notification for it.

Rules that apply to all of them:

- **You are never notified about your own action.** Your comment, your
  approval or your merge notifies other people, not you.
- **Members only.** A notification goes only to people who are members of the
  project at the moment it is sent. Someone who has watched an event and was
  later removed from the project receives nothing about it, and your
  notification list shows only projects you are currently a member of. The
  same check happens before every email.
- **Signals are rate-limited.** You get one notification for each new signal on
  what you watch, and at most one signal notification per watched entity every
  6 hours. A signal that is hidden, or already has a
  [verdict](./anomaly-detection.md#signal-verdicts), never notifies anyone.

Each notification has a short title and text and links to the place it is
about: the event, the thread, the metric or the branch.

## @mentions

In a comment box, type `@` to pick a project member from a list of the
project's members. The comment stores the mention in the form
`@[Name](user_id)`, and it is shown as a name chip. The mentioned member is
notified even if they do not watch the event.

Only mentions picked from the list count. Plain text such as `@anna` is not
read as a mention and notifies nobody. You can mention only members of the
project, and someone who has left the project by the time the comment is
posted is skipped.

## Reading notifications

Open the bell in the top bar. The **Notifications** tab lists your
notifications across all your projects, newest first, with the unread count on
the tab. Click a notification to go to what it is about. **Mark all read**
clears the unread count. The **Signals** tab shows the open incidents, active
signals and recent alert deliveries described in
[Feature reference › Top-bar bell](./feature-reference.md#top-bar-alerts-bell).

## Email

Email is the only channel outside the app for now. Set it in **Profile ›
Notifications**:

| Setting | Options | Default |
|---------|---------|---------|
| **Email frequency** | **Off** (in the app only), **Instantly** (one email per notification), **Daily digest**, **Weekly digest** | **Daily digest** |
| **Email me when I am mentioned** | on / off | on |

- **Instantly** emails each notification on its own, within about a minute of
  it being created.
- **Daily** and **Weekly** send one digest, grouping the notifications that
  are still unread and have not been emailed yet. A notification you have
  already read in the app is left out of the digest.
- **Email me when I am mentioned** controls mentions separately from the
  frequency, so you can keep a quiet digest and still hear about mentions by
  email, or turn mention emails off entirely.
- A notification is emailed at most once, whichever path sends it.
- Emails use the instance's SMTP settings. Without SMTP only in-app
  notifications work: no email is sent, the Profile section says so, and your
  choices are kept for when SMTP is set up. See
  [Configuration](../run/configuration.md).

## Related pages

- [Feature reference](./feature-reference.md): the bell, the Watch buttons and
  the Profile section
- [Agent / API Guide › Notifications and subscriptions](../integrate/agent-api-guide.md#notifications)
- [Alerting rules](./alerting.md): routing signals to channels and notifying
  owners
