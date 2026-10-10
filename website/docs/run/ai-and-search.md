---
title: AI & search providers
sidebar_position: 7
---

# AI & search providers

tripl ships with two **optional** feature groups that reach out to an external,
OpenAI-compatible provider. Both are **OFF by default** and must be explicitly
enabled — tripl runs fully without either:

1. **Semantic search embeddings** — upgrades the smart/global search
   (`GET /projects/{slug}/search`) from keyword/substring matching to
   embedding-based semantic ranking.
2. **AI assistance** — powers in-app helpers such as event-description
   suggestions on the event form.

A single shared variable, `OPENAI_API_KEY`, acts as the credential fallback for
both groups, so a minimal setup only needs one key.

:::info Why opt-in
Both groups send tracking-plan text to a third-party provider. They stay off
until an operator turns them on, so a default deployment never transmits your
catalog anywhere. See the [privacy trade-off](#the-privacy-trade-off) below.
:::

---

## Semantic search embeddings

When enabled, tripl indexes your tracking-plan text (event names, descriptions,
field and meta values, and related entities) as vector embeddings and uses them
to rank smart-search results by meaning rather than literal substring overlap.
The `/search` response then reports `semantic_used: true`. A caller can still
ask for the keyword answer alone with `semantic=false`; the command palette
does exactly that first, shows those rows, and swaps in the full answer when
it arrives, so the embedding round trip never holds the first list back.

When **disabled** — the default — `/search` still works. It transparently falls
back to keyword/substring matching and returns `semantic_used: false`. No text
leaves the instance. The one exception is the **demo project**: it ships with
precomputed embedding vectors for its own content, so demo searches can report
`semantic_used: true` without any provider configured — still with no text
leaving the instance, since those vectors are computed by maintainers ahead of
time and bundled with the release. Once embeddings are enabled, the demo is
embedded by the configured provider like any other project and the bundled
vectors are no longer used.

The `semantic_used` above is the flag on the **envelope**, and it is the one to
read when diagnosing configuration: it says the semantic leg ran. Each hit in
`items` carries its own `semantic_used`, which is narrower — that hit came from
the vector leg alone — so a correctly configured instance routinely answers
`semantic_used: true` on the envelope with every row reading `false`. Never
diagnose an instance from a row.

Interactive search gives the embedding provider three seconds. A timeout or a
vector with the wrong width leaves the keyword results available; background
embedding batches retain their longer timeout. Search snippets and highlights
preserve the match location in descriptions with indentation or Unicode case
folding. Long relation names are shortened to fit the search index.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SEARCH_EMBEDDINGS_ENABLED` | `false` | Master switch for semantic search. When `false`, `/search` uses keyword/substring fallback only. |
| `SEARCH_EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible embeddings endpoint; `/embeddings` is appended. Env-only — no instance-settings override, see the re-indexing warning below — but shown read-only, with its source badge, under **Settings → Platform → AI & search**. |
| `SEARCH_EMBEDDING_PROVIDER` | `openai` | Embedding provider. |
| `SEARCH_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model used to index and query tracking-plan text. |
| `SEARCH_EMBEDDING_DIMENSIONS` | `1536` | Fixed at 1536 by the database column. Startup rejects any other value; use a model that returns 1536 values. |
| `SEARCH_EMBEDDING_API_KEY` | falls back to `OPENAI_API_KEY` | Credential for the embedding provider. |

:::tip
Leave `SEARCH_EMBEDDING_API_KEY` unset and just provide `OPENAI_API_KEY` if the
same credential serves both embeddings and AI assistance.
:::

---

## AI assistance

AI assistance drives generative helpers in the app — for example, suggesting an
event description on the event form, or writing the cited
[incident summary](../use/alerting.md#incident-summary) on an alerting incident.
It targets an OpenAI-compatible chat endpoint.

Ask keeps the question at the start of the provider prompt, so large search
contexts cannot truncate it. Malformed provider responses and interrupted
connections produce the existing unavailable response or describe error.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_ENABLED` | `false` | Master switch for AI assistance. When `false`, AI-backed helpers return a "disabled" response. |
| `AI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible API. Point this at a proxy or self-hosted endpoint to use a different backend. |
| `AI_MODEL` | `gpt-4o-mini` | Chat/completion model. |
| `AI_API_KEY` | falls back to `OPENAI_API_KEY` | Credential for the AI provider. |
| `AI_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `AI_MAX_OUTPUT_TOKENS` | `700` | Cap on generated output length. |

:::note Bring your own endpoint
Because `AI_BASE_URL` speaks the OpenAI-compatible protocol, any compatible
gateway, proxy, or self-hosted model server works in place of OpenAI — set
`AI_BASE_URL` to its address and `AI_API_KEY` to whatever credential it expects.
:::

### Claude, Gemini and OpenRouter {#provider-presets}

The AI settings (Platform › AI & search for the operator, Organization › AI for an
organization) open with a **Provider** picker. Picking one fills **Base URL**
and **Model** with that provider's OpenAI-compatible endpoint and a small model;
both stay editable, and a model you typed yourself is kept when you switch.
Add the provider's key and save.

| Provider | Base URL | Starting model | Notes |
|----------|----------|----------------|-------|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | |
| Anthropic (Claude) | `https://api.anthropic.com/v1` | `claude-haiku-4-5` | No embeddings API: keep search embeddings on another provider. |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` | A key from Google AI Studio. Also serves embeddings (`gemini-embedding-001`). |
| OpenRouter | `https://openrouter.ai/api/v1` | `google/gemini-2.5-flash` | Models are `vendor/model`. tripl sends OpenRouter's optional `X-Title` and `HTTP-Referer` (your `APP_BASE_URL`) headers. |

Any other address shows as **Custom**. Where a provider's compatible endpoint
refuses a parameter tripl sends (structured `response_format`, `temperature`,
`max_tokens`), tripl drops or renames that parameter and retries once per
parameter, whether the provider reports it OpenAI-style or only in the error
message (Anthropic, Gemini).

With environment variables only, set the same pair:

```bash
AI_BASE_URL=https://api.anthropic.com/v1
AI_MODEL=claude-haiku-4-5
AI_API_KEY=sk-ant-...
```

Set `AI_API_KEY` explicitly for a non-OpenAI provider: its fallback,
`OPENAI_API_KEY`, would otherwise be sent to that provider.

---

## The privacy trade-off

Enabling either group means tripl transmits indexed tracking-plan text to the
configured provider:

- **Embeddings** send event names, descriptions, field/meta values and related
  entity text to the embedding provider so they can be turned into vectors for
  semantic ranking.
- **AI assistance** sends the relevant event context to the chat model to
  generate suggestions.
- **Incident summaries** send the project name and a numbered list of at most
  20 short facts about one incident: scope, rule and event names, rounded
  counts, attribution headlines, release versions, past verdicts and their
  notes, the incident note and recent event comments. Values of fields marked
  sensitive are replaced with `(redacted)`. Drift sample values, raw events,
  and the names of whoever wrote a verdict or comment are never sent. Demo
  projects never send any. What people typed into notes and comments is
  sent as written. See
  [What is sent to the model](../use/alerting.md#what-is-sent-to-the-model-and-what-never-is).

This is precisely why both default to **OFF**: you opt in knowingly.

:::warning
With embeddings disabled, search is **not** broken — it degrades gracefully to
keyword/substring matching (`semantic_used: false`, except in the demo project,
which uses bundled precomputed vectors). Many deployments run this way
indefinitely. Only enable embeddings if you accept sending tracking-plan text
to the provider in exchange for semantic relevance.
:::

If your provider is a self-hosted or in-VPC OpenAI-compatible endpoint, you can
keep both features on while keeping all text inside your own infrastructure —
point `SEARCH_EMBEDDING_BASE_URL` and `AI_BASE_URL` at that endpoint. Both take a
**base**, not a full path: tripl appends `/embeddings` and `/chat/completions`
respectively, the way an OpenAI-compatible server lays them out.

:::warning Changing the embedding space
`SEARCH_EMBEDDING_BASE_URL` is env-only on purpose, and so is
`SEARCH_EMBEDDING_DIMENSIONS`. The database accepts only 1536-dimensional
vectors. Search uses vectors only when their recorded endpoint and model match
the current configuration. Changing either makes old vectors ineligible; the
scheduled reindex visits every branch and queues fresh embeddings. Until that
finishes, affected searches use their keyword results. This re-embedding calls
the configured provider and may take several sweep cycles for large projects.
:::

:::info Per-organization embeddings
These variables are the **operator's** values. Each organization may use its
own endpoint, provider, model and key instead (**Settings → Organization →
Search**), and each organization's vectors are stamped and queried in its own
space: the endpoint and model it runs with. An organization that sets none
inherits the operator's (or, with `ORG_SETTINGS_OPERATOR_FALLBACK=none`, has
semantic search off). Saving an organization's own model runs a test embedding
and is refused unless it returns 1536 values; the change then reindexes that
organization's projects only. The stale sweep and the stranded-embedding chaser
decide per organization. See
[Operator and organization settings](./configuration.md#operator-and-organization-settings).
:::

---

## How to set these

### Compose / environment

Like all backend settings, these are read from the process environment or a
`.env` file. Under Docker Compose, set them in the `.env` next to
`compose.yaml`: its shared `x-app-environment` anchor already forwards every
`AI_*` and `SEARCH_EMBEDDING*` variable, and `OPENAI_API_KEY`, to every service
that runs the app image (`app` serves the API while `celery-worker` runs the
embedding task, and both read these settings). Restart with
`docker compose up -d` after a change.

```bash
# Semantic search embeddings (opt-in)
SEARCH_EMBEDDINGS_ENABLED=true
SEARCH_EMBEDDING_BASE_URL=https://api.openai.com/v1
SEARCH_EMBEDDING_PROVIDER=openai
SEARCH_EMBEDDING_MODEL=text-embedding-3-small
SEARCH_EMBEDDING_DIMENSIONS=1536

# AI assistance (opt-in)
AI_ENABLED=true
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini
AI_TIMEOUT_SECONDS=30
AI_MAX_OUTPUT_TOKENS=700

# Shared credential fallback for both groups
OPENAI_API_KEY=sk-...
```

The anchor is an explicit allowlist, not a mount of your `.env`: a variable it
does not name reaches nothing inside the container, the application default wins
instead, and nothing is logged about it. For a variable outside this page, check
the anchor before concluding that a value you put in `.env` took effect.

Keep the key out of the repository: `.env` is not committed. See the
full [Configuration reference](./configuration.md) for how settings are loaded.

### Settings → Platform → AI & search

Operators can also review and adjust this configuration from the running
instance under **Settings → Platform → AI & search** (for example, at
`https://tripl.example.com/settings/instance/ai`). The same page governs both
the AI-assistance settings and the embeddings toggle, with the environment
variables above acting as defaults that the stored overrides can replace at
runtime — so you can confirm the active model and flip features on or off
without redeploying.

**Embeddings base URL** and **Embedding dimensions** appear there too, read-only:
neither takes an override, because the vectors already in the index were written
against one endpoint at one width and similarity across two embedding spaces is
meaningless. They are shown because their **source badge** answers a question
nothing else in the running system did. **Env** on the base URL means something
delivered `SEARCH_EMBEDDING_BASE_URL` to this container; **Default** means the
value equals the built-in `https://api.openai.com/v1`, which is either because
nothing delivered it or because what was delivered says the same thing.

That is how you verify from a browser that a self-hosted
`SEARCH_EMBEDDING_BASE_URL` actually reached the process, which is the failure
the `x-app-environment` allowlist warning above describes and which is otherwise
silent. A base URL you pointed at a local endpoint and that reads **Default** in
the browser did not arrive, and every indexed event name, description and field
value is going to OpenAI instead.

---

## Related

- [Configuration reference](./configuration.md) — every environment variable.
- [Admin guide](../administer/admin-guide.md) — instance settings and operator tasks.
- [Searching from the API](../integrate/searching-from-the-api.md) — how smart search and the structured listing differ in practice.
