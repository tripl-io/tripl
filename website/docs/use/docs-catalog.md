---
title: Docs catalog
sidebar_position: 14
---

# Docs catalog

The **Docs** page (under **Plan** in the sidebar) holds Markdown notes that
sit next to the tracking plan. People and AI agents both read them. Typical
notes are warehouse gotchas, event query recipes, naming conventions, or an
agent skill that teaches an assistant how to query your events.

## Two levels: project and organization

Every project shows two roots:

- **Project notes** belong to this project only.
- **Organization notes** belong to the project's organization. Every project of
  that organization shows the same organization notes, under their own root.

The same path can exist once in each root. Links, search and back-links cover
both roots.

## Files and folders

A note is a Markdown file with a path such as `guides/warehouse-gotchas.md`.
Folders come from the paths, so there are no empty folders. To create a folder,
create a note inside it. Renaming or moving a folder moves every note under it,
and deleting a folder deletes every note under it.

Path rules:

- The path is relative and ends in `.md`. `SKILL.md`, `README.md` and
  `references/*.md` are all allowed.
- Each folder or file name starts with a letter or digit and uses only
  letters, digits, spaces and `. _ ( ) + -`, with at most 128 characters.
- There is no `.` or `..`, no leading `/`, no `\` and no control characters.
  `a//b.md` becomes `a/b.md`, and a leading `./` is removed.
- A path has at most 10 levels and 512 characters.
- Paths are **case-insensitive** within a root. `Guide.md` and `guide.md`
  cannot both exist, so an export always unpacks on macOS and Windows.

## Frontmatter

A note may start with a YAML block. tripl reads these keys:

```markdown
---
title: Warehouse gotchas
description: Why the events table double counts some sessions
tags: [warehouse, sql]
audience: agent
---
```

| Key | Meaning |
| --- | --- |
| `title` | The note's title. If it is missing, tripl uses `name` (which agent-skill files use), then the first `# Heading`, then the file name. At most 300 characters. |
| `description` | A short summary shown in the tree and in search. At most 2000 characters. |
| `tags` | A list, or a comma-separated string. At most 30 tags, each at most 64 characters, using letters, digits and `_ . : -`. |
| `audience` | `human`, `agent` or `both` (the default). It is a label for filtering. It does not hide the note from anyone. |

tripl stores every other key unchanged and does not check it, so
`allowed-tools`, `metadata` and other agent-skill keys survive an import and
export. Frontmatter that is not valid YAML, or that is not a mapping, is
refused with a message that names the problem.

## Links to the plan

A note can link to plan entities by name:

| Syntax | Links to |
| --- | --- |
| `[[event:purchase]]` | the event named `purchase` |
| `[[event-type:checkout]]` | the event type named `checkout` |
| `[[field:amount]]` | a field named `amount` on any event type |
| `[[field:checkout/amount]]` | the field `amount` of event type `checkout` |
| `[[event:purchase\|the purchase event]]` | the same link, shown as *the purchase event* |

Links resolve against the **main** plan every time a note is shown, so a link
never points at an old copy. Links inside code blocks and inline code are
plain text. When no entity has the name, the link is **broken**. The note
shows a warning that lists the broken links, and a save reports them too, but
it is still saved. When more than one event has the name, the link is
**ambiguous** and opens the first one.

On the main plan, the event and event-type pages show a **Notes** card that
lists every note that links to them, from both roots. An event type's
**Field notes** section lists the notes that link to its fields.

## History

Every save keeps a revision with its author, time and full content. The
**History** drawer lists the revisions, shows the diff of each one against
the revision before it, and can **Restore** one. A restore is a new revision,
so it can be undone too. It brings back the content, not the old path.

Deleting a note also deletes its history. The audit log keeps a record of the
delete: the path, the last revision number and a hash of the content of every
note removed, including each note a folder delete or a mirror import removed.
A deleted note also leaves search at once, on every plan branch.

Saving checks the revision you started from. If someone else saved in the
meantime, you get a conflict and can reload their version or overwrite it.

## Who can read and write

- Every project member can read the project's notes and its organization's
  notes. This includes viewers and `read`-scope API keys.
- Project editors (and the instance owner) can write project notes.
- Organization notes can be written by instance editors and owners who can
  edit the project they are working in. An API key bound to one project can
  read organization notes but cannot change them, because other projects read
  them too.
- Deleting organization notes in bulk, with a folder delete or an import in
  **mirror** mode, needs the instance owner signed in to the web app. An API
  key cannot do it, whoever owns it.

Every change is recorded in the project's **Audit** tab, in the **Docs** group.

## Import and export

A root can be exported as a zip of `.md` files or as a JSON bundle. The export
holds each note's content exactly as stored, frontmatter included, so an export
followed by an import changes nothing.

An import takes a zip or a JSON bundle:

- Only `.md` files are imported. Other files, such as `scripts/`, images or
  `assets/`, are listed as **skipped** and never cause a failure.
- A zip whose entries all sit in one top-level folder (for example
  `my-skill/SKILL.md` and `my-skill/references/schema.md`) is unpacked without
  that folder unless you choose to keep it. So an agent-skill folder imports as
  `SKILL.md` plus `references/`.
- **Merge** creates and updates notes. **Mirror** also deletes the notes that
  the import does not contain.
- A **dry run** shows what would be created, updated, left unchanged, deleted,
  skipped or refused, and changes nothing.
- An import with any error (a bad path, invalid frontmatter, a note that is too
  large, or two paths that differ only in case) changes nothing and lists the
  errors.

The **Import / export** button on the Docs page does both. It picks the root,
exports a zip or JSON, and imports a `.zip` or `.json` file with **Merge** or
**Mirror**. **Import** is enabled only after **Preview (dry run)** has run
without errors. Only the instance owner, signed in to the web app, can mirror
organization notes.

### Layout of an export

A zip export is named `<project-slug>-docs.zip` for project notes and
`<organization-slug>-docs.zip` for organization notes. Every note is one entry
at its own path, with its content exactly as stored. The zip has no index file
and no wrapper folder. `tripl docs pull` writes the same layout into a local
folder.

### Example: an agent skill

An agent skill is a folder with a `SKILL.md` and some reference notes. It can
also hold scripts. Here is a generic skill that teaches an assistant how to
query events:

```text
event-query-recipes/
├── SKILL.md
├── references/
│   ├── warehouse-gotchas.md
│   └── funnel-queries.md
└── scripts/
    └── run_query.py
```

`SKILL.md` uses the usual skill frontmatter. tripl takes the title from `name`
and keeps `allowed-tools` unchanged:

```markdown
---
name: event-query-recipes
description: How to query tracked events in the warehouse without double counting
allowed-tools: [Read, Bash]
audience: agent
tags: [warehouse, sql]
---

# Event query recipes

Read references/warehouse-gotchas.md before you write a query.
Each recipe names its events, such as [[event:checkout_started]] and
[[event:purchase]], so the links show which plan entries it depends on.
```

`references/warehouse-gotchas.md` is an ordinary note:

```markdown
---
title: Warehouse gotchas
audience: both
---

- A retried request is sent again with the same `event_id`. Deduplicate on it
  before you count.
- [[field:checkout/amount]] is in cents.
```

Zip the folder and import it into a root. Every Markdown entry sits in the
top-level folder `event-query-recipes/`, so tripl removes that folder:

| Entry in the zip | Result |
| --- | --- |
| `event-query-recipes/SKILL.md` | created as `SKILL.md` |
| `event-query-recipes/references/warehouse-gotchas.md` | created as `references/warehouse-gotchas.md` |
| `event-query-recipes/references/funnel-queries.md` | created as `references/funnel-queries.md` |
| `event-query-recipes/scripts/run_query.py` | skipped: not a Markdown (.md) file |

To keep the folder, check **Keep the zip's top-level folder** in the dialog.
In the API this is `keep_root=true`, and in the CLI it is
`tripl docs push --keep-root`. The notes are then stored as
`event-query-recipes/SKILL.md` and so on, so several skills can share one
root. The same import from a local folder with the CLI:

```bash
tripl docs push ./event-query-recipes --project shop --scope project
```

An export of that root contains `SKILL.md` and the two `references/` notes,
with their frontmatter unchanged. Unpack it into a folder and an agent can use
it as a skill again. `scripts/run_query.py` is not in the export, because
tripl never stored it. Keep files that are not Markdown somewhere else, such as
a git repository.

## Limits

| Limit | Value |
| --- | --- |
| One note | 256 KiB of UTF-8 |
| Frontmatter block | 16 KiB |
| Notes per root | 5000 |
| Files in one import | 2000 |
| Total size of one import | 20 MiB (uncompressed) |
| Zip upload | 10 MiB, with a compression ratio of at most 100:1 per file |
| Plan links per note | 500 |

## Search

Notes appear in the command palette's search, and in the plan search that the
AI assistant and agents use, as the **Docs** type. A result opens the note.
Search covers the title, description, tags, path, body and linked names.

## For agents

Agents use the same notes through the API (`/projects/{slug}/docs`), the MCP
tools `list_docs`, `read_doc`, `search_docs` and `write_doc`, and the CLI
(`tripl docs ls`, `cat`, `pull` and `push`). See the
[Agent API guide](../integrate/agent-api-guide.md#docs-catalog).
