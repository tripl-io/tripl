# tripl documentation site

Built with [Docusaurus](https://docusaurus.io/). Published to GitHub Pages at
https://vladenisov.github.io/tripl/ by `.github/workflows/docs.yml`.

## Local development

This package uses pnpm via [corepack](https://nodejs.org/api/corepack.html).
Install with `--ignore-scripts` (a transitive `core-js` postinstall is only a
funding notice and is not needed to build the site):

```bash
corepack pnpm install --frozen-lockfile --ignore-scripts
corepack pnpm start      # dev server with hot reload
corepack pnpm build      # production build into build/
corepack pnpm serve      # serve the production build locally
```

`pnpm-workspace.yaml` sets `verifyDepsBeforeRun: false` so `pnpm build` does not
re-trigger an install on every run.

## Structure

Content lives in `docs/`, grouped by audience (sidebar order and labels come
from each folder's `_category_.json`):

- `how-to/` — task-sized guides with screenshots, one job per page
- `use/` — using tripl (concepts, user guide, variables, monitoring, alerting,
  troubleshooting)
- `use-cases/` — task-specific guides for search and agent-driven workflows
- `administer/` — instance administration & settings
- `run/` — self-hosting, deployment, operations, release process
- `develop/` — architecture & contributing
- `integrate/` — API & integration guide and the generated OpenAPI reference

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
Regenerate that spec from the FastAPI app after API changes:

```bash
./bin/dump-openapi.sh   # writes website/openapi/tripl.openapi.json
```
