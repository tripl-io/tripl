# Docs screenshots

The screenshots on the docs site are taken by a script, not by hand, so they can
be retaken in a few minutes whenever the app changes. `capture.mjs` signs in to
a running tripl, generates a fresh demo project (replacing any earlier one),
adds a few notes to its Docs, and photographs every shot in `shots.mjs` in the
light and the dark theme. The pictures land in
`../static/img/screenshots/<id>.<theme>.webp`.

## Retake them

1. Run tripl locally: the dev stack (`docker compose -f compose.dev.yaml up`) or
   the backend, worker and Vite dev server from `CONTRIBUTING.md`. Use a
   throwaway database: the script creates an account and deletes and recreates
   the demo project.
2. Install this folder's one dependency (it has its own lockfile, apart from the
   docs site):

   ```bash
   cd website/screenshots
   bun install --frozen-lockfile
   ```

3. Capture:

   ```bash
   TRIPL_URL=http://localhost:5173 bun run capture
   ```

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `TRIPL_URL` | `http://localhost:5173` | The app. |
   | `TRIPL_EMAIL`, `TRIPL_PASSWORD` | a demo owner | Signs in, or registers it on an instance with no users yet. |
   | `CHROME_PATH` | Chromium or Chrome in the usual places | The browser; `puppeteer-core` brings none. |
   | `THEMES` | `light,dark` | Which themes to take. |
   | `ONLY` | every shot | Comma-separated shot ids, to retake a few. |

4. Look at the pictures before you commit them, and commit them with the change
   that made them necessary.

The demo chrome (the synthetic-data banner, the welcome panel, chapter hints) is
hidden in every shot except `demo-welcome`, so the pages look as they do in a
real project. Toasts are hidden and motion is off.

## Add a shot

Add an entry to `shots.mjs`: an `id`, the `url` to open (built from the demo
project in `ctx`), and optionally `prepare` to click or type first, `clip` to crop
to one element, or `showDemo: true` to keep the demo chrome. Then use it in a page:

```md
![What the picture shows](/img/screenshots/<id>.light.webp#gh-light-mode-only)
![What the picture shows](/img/screenshots/<id>.dark.webp#gh-dark-mode-only)
```

Docusaurus shows the image that matches the reader's theme. The alt text is what
a screen reader says and what shows if the image does not load, so describe what
the picture shows rather than naming it. A page that names a missing picture
fails the docs build.
