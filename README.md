# Tripl

<p align="center">
  <img src="assets/tripl-logo.svg" alt="tripl" width="220" />
</p>

<p align="center"><strong>Keep your product analytics honest.</strong></p>

<p align="center">
  <a href="https://vladenisov.github.io/tripl/">Docs</a> ·
  <a href="website/docs/quick-start.md">Quick start</a> ·
  <a href="website/docs/use/demo-workspace.md">Try the demo</a>
</p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="website/static/img/screenshots/event-monitoring.dark.webp" />
  <img src="website/static/img/screenshots/event-monitoring.light.webp" alt="An event in tripl the morning after a release: a +197% spike flagged against its baseline, the week of volume behind it, a verdict to give, and the breakdown showing 85% of the jump comes from iOS" />
</picture>

---

## A Tuesday morning, with and without tripl

A release went out last night. By morning, *Home Screen View* is firing three
times as often as it should.

**Without tripl**, nobody notices. Two weeks later a PM asks why engagement
jumped, an analyst spends a day in SQL, and the answer turns out to be an iOS
build that logs the screen twice. Every report built on those two weeks is
quietly wrong.

**With tripl**, a message lands in your team's channel that morning: *volume
spike, +197% against the usual for this hour*. You open it and see the screen
above. The jump is real, and 85% of it comes from iOS. You mark it **Tracking
bug**, the event's owner hears about it, and the fix ships the same day.

That's the whole idea. tripl keeps a written-down **tracking plan** of what your
product *should* send, compares it with what your apps *actually* send, and
tells you the moment the two disagree.

**It works with the data you already have.** tripl connects to your existing
warehouse — **ClickHouse**, **BigQuery**, or **PostgreSQL** — and reads the
events already landing there. There's no SDK to ship and nothing to
re-instrument, and tripl only ever reads from your warehouse.

It's built for **product managers, analysts, and data engineers** who own a
tracking plan. If you've ever heard about broken analytics from a stakeholder
before a tool told you, it's for you.

---

## Try it in two minutes, no warehouse needed

```bash
cp .env.example .env
docker compose -f compose.dev.yaml up --build
```

Open <http://localhost:5173>, create the first account, and click **Generate
demo project**. In about fifteen seconds you have a realistic project: events,
fields, a week of metrics, a few anomalies (the spike above among them), and a
guided tour through it.

The demo's data comes from a **local synthetic warehouse**, so nothing leaves
your machine. Everything else is the real product. Scans, metric collection,
anomaly detection, and reconciliation all actually run, and a background clock
keeps the data fresh. [The demo workspace](website/docs/use/demo-workspace.md)
lists exactly what is synthetic.

When you're ready for your own data, add your warehouse under **Settings → Data
sources** and follow the **[Quick Start guide](website/docs/quick-start.md)**,
from a first scan to your own metrics and a working alert.

---

## What's inside

tripl is organised around three jobs: **Plan**, **Observe**, and **Govern**.

<table>
  <tr>
    <td width="33%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="website/static/img/screenshots/branches.dark.webp" />
        <img src="website/static/img/screenshots/branches.light.webp" alt="A plan branch under review: its status, the change it makes, and what it would affect downstream" />
      </picture>
      <p><strong>📐 Plan</strong><br/>Every event, field, and value in one searchable catalog. Changes go on a <em>branch</em> and are reviewed before they merge, like a pull request for your tracking plan.</p>
    </td>
    <td width="33%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="website/static/img/screenshots/overview.dark.webp" />
        <img src="website/static/img/screenshots/overview.light.webp" alt="A project's Overview: open signals, plan coverage, a week of volume and the busiest events" />
      </picture>
      <p><strong>📊 Observe</strong><br/>Anomaly detection that learns each event's daily and weekly rhythm, plus schema drift, release regressions, and a metrics catalog. Every signal shows <em>which slice</em> of the data moved.</p>
    </td>
    <td width="33%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="website/static/img/screenshots/reconciliation.dark.webp" />
        <img src="website/static/img/screenshots/reconciliation.light.webp" alt="Reconciliation: how much of the real data matches the plan, events seen but not documented, and documented events that stopped arriving" />
      </picture>
      <p><strong>🛡️ Govern</strong><br/>Reconciliation answers two questions: <em>what's documented but no longer arriving</em>, and <em>what's arriving but was never documented</em>. Coverage, an audit log, and roles round it out.</p>
    </td>
  </tr>
</table>

A few more things worth knowing:

- **Scans bootstrap the plan for you.** Point a scan at a real table and tripl
  proposes the events, fields, and value lists it finds there.
- **Alerts go where your team already is**: Slack, Telegram, email, a webhook,
  Jira, or Linear. Before turning a rule on, you can replay recent data through
  it to see how noisy it would be.
- **Scripts and AI agents are welcome.** Revocable API keys, a CLI
  (`pip install tripl`), and an MCP server (`tripl-mcp`) let an LLM search the
  plan and propose changes on a branch, with exactly the permissions you give it.

[Concepts](website/docs/use/concepts.md) explains how it all fits together, in
plain language.

---

## Running it for real

The default `compose.yaml` runs the published release image: set your secrets,
run `docker compose up -d`, and the app comes up on `:8000`. The
**[deployment guide](website/docs/run/deployment.md)** covers the rest, and
cutting a release is one command, `bin/release.sh` (see the
**[release process](website/docs/run/release.md)**).

| Local dev stack | URL |
|---|---|
| App | http://localhost:5173 |
| API | http://localhost:8000 |
| API reference (interactive) | http://localhost:8000/docs |

---

## Documentation

📖 The full documentation lives at **[vladenisov.github.io/tripl](https://vladenisov.github.io/tripl/)**
(sources under [`website/docs/`](website/docs)). Good places to start:

- **[Quick Start](website/docs/quick-start.md)** — from `docker compose up` to
  a scanned plan, your own metrics, and a first alert.
- **[Concepts](website/docs/use/concepts.md)** — tracking plans, events,
  branches, and monitors, in plain language.
- **[User guide](website/docs/use/user-guide.md)** — a hands-on walkthrough from
  your first project to a working alert.
- **[Variables & templates](website/docs/use/variables-and-templates.md)** —
  documented values, source bindings, per-event overrides, and value drift.
- **[Agent & API guide](website/docs/integrate/agent-api-guide.md)** — letting an
  LLM agent or a script read and update the plan.

---

## Contributing

tripl is a FastAPI + PostgreSQL backend, a Celery worker that talks to your
warehouses, and a React frontend, all runnable locally with Docker Compose.

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — local setup and commands. The root
  `Makefile` collects the common ones; run `make` to list them.
- **[Architecture](website/docs/develop/architecture.md)** — how the system is
  built, and why.
- **[AGENTS.md](AGENTS.md)** — a navigation map of the repo for coding agents.
