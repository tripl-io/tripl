# tripl documentation site

Built with [Docusaurus](https://docusaurus.io/). Published to GitHub Pages at
https://docs.tripl.io/ by `.github/workflows/docs.yml`.

## Local development

Bun is the package manager and runtime (version pinned in `package.json`
"packageManager"). Dependency lifecycle scripts do not run unless listed in
"trustedDependencies"; none is, as the site needs none (a transitive `core-js`
postinstall is only a funding notice).

```bash
bun install --frozen-lockfile
bun run start        # dev server with hot reload
bun --bun run build  # production build into build/
bun run serve        # serve the production build locally
```

## Structure

Content lives in `docs/`, grouped by audience (sidebar order and labels come
from each folder's `_category_.json`):

- `how-to/` — task-sized guides with screenshots, one job per page
- `use/` — using tripl (concepts, user guide, variables, monitoring, alerting,
  troubleshooting)
- `administer/` — instance administration & settings
- `run/` — self-hosting, deployment, operations, release process
- `enterprise/` — features of the Enterprise edition (`editions.md` says what
  each edition has)
- `develop/` — architecture, extension points and the warehouse capability matrix
- `integrate/` — API & integration guide, agent API guide, searching from the
  API, MCP server, codegen, tripl-check, and the generated OpenAPI reference

## Screenshots

Screenshots of the app live in `static/img/screenshots/`, one per theme
(`<id>.light.webp`, `<id>.dark.webp`), and are taken by the script in
`screenshots/` from a running tripl with a demo project. Do not edit them by
hand: retake them with the script when the UI they show changes. See
[screenshots/README.md](screenshots/README.md).

## Notes

- `markdown.format: 'detect'` → `.md` files are CommonMark (literal `${var}` /
  `{slug}`), `.mdx` is reserved for interactive/OpenAPI pages.
- `onBrokenLinks: 'throw'` — any broken internal link fails the build.

## API reference

The `/integrate/api` page is a [Redoc](https://github.com/Redocly/redoc) render of
`openapi/tripl.openapi.json` (via [redocusaurus](https://github.com/rohit-gohri/redocusaurus)).
That file is generated, never edited by hand. Regenerate it from the repo root
after API changes:

```bash
make sync-types   # writes it together with backend/openapi.json and the frontend's api.gen.ts
```

It keeps the app's own order (Redoc lists operations in document order), while
`backend/openapi.json` has sorted keys. CI fails when the two say different
things (`bin/check-openapi-docs.py`).
