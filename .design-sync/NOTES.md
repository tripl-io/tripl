# design-sync NOTES — Tripl design system → claude.ai/design

Project: **Tripl** — the design project id lives in `config.json` as `projectId`,
which is where the tooling reads it. It is an identifier for a private project,
not a credential, and it is not repeated here or written as a browsable link.
Shape: **package** (synth-entry — `frontend` is a private app, no published lib build).
Upload path: **incremental** (project created empty this run).

## Scope (user-chosen, 2026-06-20)
- Components: UI primitives + custom primitives + **app composites** (sidebar, top-bar, command palette infra, settings kit, monitoring panels). 56 components, see `config.json` `componentSrcMap`.
- Previews: **author all scoped** (rich graded previews for every component).
- Fonts: **ship Inter + JetBrains Mono from Google Fonts (OFL)** — not yet wired (see TODO).

## Build commands (run from repo root)
```sh
# 1. compile Tailwind utility CSS (cfg.buildCmd) — regenerate before each build
.ds-sync/node_modules/.bin/tailwindcss -i .design-sync/tw-input.css -o frontend/.ds-styles.css
# 2. converter build
node .ds-sync/package-build.mjs --config .design-sync/config.json \
  --node-modules ./frontend/node_modules \
  --entry ./frontend/.ds-entry.tsx --out ./ds-bundle
# 3. validate (render check — needs playwright+chromium, see TODO)
node .ds-sync/package-validate.mjs ./ds-bundle
```
Deps staged in `.ds-sync/node_modules` (esbuild, ts-morph, @types/react, @tailwindcss/cli). Use the node version `frontend/package.json` pins; a mismatched ambient node has run this fine, but do not rely on that.

## Durable build inputs (committed)
- `.design-sync/config.json` — full config (projectId, componentSrcMap of 56, entry, cssEntry, tsconfig, buildCmd).
- `frontend/.ds-entry.tsx` — curated barrel (bundle entry). Resolves the `Select` name collision (ui/select vs settings/kit both export `Select` — kit's is omitted; ui's wins).
- `frontend/tsconfig.dssync.json` — comment-free alias tsconfig (see gotcha #1).
- `.design-sync/tw-input.css` — Tailwind compile input (imports app index.css, @source src + previews).
- `.design-sync/previews/<Name>.tsx` — authored previews (TODO, none yet).

## Gotchas / fixes already applied
1. **tsconfig comment-strip bug (converter lib).** `lib/bundle.mjs` `tsconfigPathsPlugin` strips block comments with a naive regex; the `/*` inside path aliases `@/*` and `./src/*` plus a later real `*/` comment make it DELETE the whole `paths` block → plugin returns NULL → every `@/` import fails to resolve. **Fix:** point `cfg.tsconfig` at `frontend/tsconfig.dssync.json` (comment-free, so no `*/` to trip the regex). Do NOT use `tsconfig.app.json` directly.
2. **`@/types` is a directory import.** The alias plugin returns the bare dir before trying `/index.ts` → esbuild "is a directory". **Fix:** exact mapping `"@/types": ["./src/types/index.ts"]` BEFORE the `"@/*"` wildcard in tsconfig.dssync.json.
3. **CSS is Tailwind v4** — no shipped compiled stylesheet. Must compile `frontend/.ds-styles.css` via cfg.buildCmd (step 1) before the converter; cssEntry points at it. It's gitignored (regenerated each build); scans src + previews so preview utility classes are included.

## OPEN ISSUES / TODO (in priority order)
1. **.d.ts props are STUBS (`[key: string]: unknown`).** Synth mode: the converter's ts-morph project only globs `**/*.d.ts` (zero in an app) and never loads `.tsx`, so prop extraction can't work — every component emits stub props. The `.d.ts` is the contract the design agent codes against, so this hurts fidelity. Options being considered:
   - (a) Generate real `.d.ts` via `tsc --emitDeclarationOnly` for the barrel/components into a types tree the converter can read (needs the converter to find it as typesRoot — no direct cfg knob; may need pkgJson `types` or a generated dist). Resolves all 56 at once if wired.
   - (b) `cfg.dtsPropsFor.<Name>` hand-written bodies. Reliable, contained.
   - **DECISION: (b), fused into preview authoring.** Each preview subagent reads the component source anyway → it ALSO records a real `<Name>Props` body in its learnings file. Orchestrator collects them into `cfg.dtsPropsFor` and rebuilds. Prioritize UI + custom primitives (the reusable core, ~30); app composites/charts/monitoring may keep stubs (app-specific props; previews+prompts carry usage).

## STATUS (first clean validate passed — incremental gate)
- Build + validate exit 0. 56 components, fonts shipped, render check runs on playwright chromium. 23 floor cards (all unauthored — every component is author-scoped per user).
- **Upload channel OPEN** (approved; the planId is session-scoped and is not recorded here — re-run finalize_plan with the §3 writes/deletes globs, localDir ./ds-bundle).
- **Authored-preview import convention:** preview `.tsx` files import the DS package by name — `import { Button, Card } from 'frontend'` (the story-imports plugin redirects `frontend`/`frontend/X`/alias `@/components/...`-to-known-components onto `window.Tripl`). Each named export in the `.tsx` = one card cell. Do NOT add the `@dsCard` header (converter adds it). Real props/children, realistic content, no foo/bar.
- NEXT WORKFLOW:
  1. Open upload channel (finalize_plan, one approval) — see base SKILL §3. planId lives for session.
  2. CALIBRATION: author 2-3 previews end-to-end (1 simple e.g. Button, 1 compound e.g. Card/Tabs, 1 text-heavy e.g. Field/InfoRow) to learn provider/blank-card fixes + the dtsPropsFor pattern. (May delegate to one calibration subagent.)
  3. FAN OUT preview subagents over disjoint component sets. SUBAGENT CONTRACT: edit ONLY their `.design-sync/previews/<Name>.tsx` + their `.design-sync/.cache/review/<Name>.grade.json` + their own `.design-sync/learnings/<BATCH>.md`; record needed `dtsPropsFor` bodies + any config/provider needs in learnings (NOT config/NOTES — orchestrator-only); rebuild ONLY via `node .ds-sync/lib/preview-rebuild.mjs --config .design-sync/config.json --node-modules ./frontend/node_modules --out ./ds-bundle --components <theirs>` then `node .ds-sync/package-capture.mjs --out ./ds-bundle --components <theirs>`; never run package-build/validate; never capture unscoped; grade each cell from the captured sheet on the absolute rubric.
  4. After each wave: orchestrator applies learnings (dtsPropsFor → config, provider/css fixes), recompile Tailwind CSS (`cfg.buildCmd`) so preview classes are included, full `package-build` + `package-validate`, then push verified batch (write components/<group>/<Name>/ + _preview/<Name>.*, re-arm sentinel). First push also carries shared base files (_ds_bundle.js, _ds_bundle.css, styles.css, README.md, _vendor/**, tokens/**, fonts/**, guidelines/**).
  5. Author conventions header (.design-sync/conventions.md, set readmeHeader), driver rebuild, then close-out (full writes + reconciliation deletes + sentinel + _ds_sync.json last).
## PROGRESS LOG
- Calibration (6) + Wave 1 (24: leaf UI, compound/primitives, settings kit) = **30 components authored, all cells graded good**, dtsPropsFor applied to config, build+validate clean, **PUSHED to project as first batch** (sentinel→shared base [_ds_bundle.js/.css, styles.css, README, _vendor, fonts]→150 component/preview files→sentinel). 30 components now live.
- Wave 2 (remaining 26) launched: overlays (Dialog/AlertDialog/DropdownMenu/Popover/Tooltip/Sheet/Select), charts/special (MetricsChart/MetricsMultiSeriesChart/MiniMetricsChart/Sparkline/Toaster), app composites + monitoring + SettingsLayout. Overlay components need `cfg.overrides.<Name>={cardMode:'single', viewport}` (orchestrator applies from agent reports). App composites may need provider wrappers (MemoryRouter/QueryClientProvider/ThemeProvider, imported directly in the preview) or end as floor cards.
- Batch-1 file list cached at `.design-sync/.cache/batch1-files.json`. write_files REQUIRES localPath (path-only is rejected); path==localPath for all.

## App composites likely needing providers/data (candidates for floor card or provider wiring):
- (was)  AppSidebar, TopBar, BranchSwitcher, CommandPalette infra, ActivityPanel, SettingsLayout, monitoring (TopMoversPanel/ReleaseRegressionPanel/SeasonalityHeatmap), EventPhotosSection, VariableValueContextTrigger. ThemeProvider is bundled (usable as cfg.provider).
2. **Fonts** — DONE. Inter (400/500/600/700) + JetBrains Mono (400/500), latin + latin-ext, fetched to `.design-sync/fonts/` (12 woff2 + fonts.css). `cfg.extraFonts: ["../.design-sync/fonts/fonts.css"]`. Validate no longer reports `[FONT_MISSING]`; woff2 copied into `ds-bundle/fonts/`. (Both families are variable fonts upstream, so per-weight files are byte-identical — harmless.)
   - Also FIXED: `[BUNDLE_EXPORT]` for `ConfirmDialog`/`EventPhotosSection` — they're `export default`; barrel now uses `export { default as X }`.
3. **Render check / playwright** — RESOLVED to "install playwright chromium". The system `/usr/bin/chromium` on this class of ARM board is a hardware build with **no headless support** (`Invalid ozone platform: headless`); no xvfb, no headless-shell. `validate` honors `DS_CHROMIUM_PATH` but the system binary can't run headless, so that path is dead. Network IS available → installing playwright's own chromium (arm64) to `~/.cache/ms-playwright` (background task). Once present, run validate/capture normally (no DS_CHROMIUM_PATH needed). If playwright chromium fails on missing system libs, try `npx playwright install-deps chromium` (needs apt/sudo) or ask user.
   - Decision rationale: user chose full high-fidelity sync, which requires a working headless browser for render-check + preview grading; the system browser is unusable; network available → install is the prerequisite, not optional.
4. **Author previews** — none yet. Solo-author 2-3 (simple/compound/state-heavy/text-heavy) to calibrate, then fan out subagents over disjoint sets. App composites (AppSidebar, TopBar, CommandPalette infra, monitoring panels) need providers (theme/branch/router/query) or data — many will be floor cards; record which here as decided.
5. **Grouping** — ui + root app components land in group `general` (source-kit derives group from dir; `ui`/`components` are generic). primitives/settings/monitoring get real groups. Cosmetic; could refine via docsMap category stubs (costs synthesized prompt) — deferred.

## FINAL — first sync COMPLETE (2026-06-20)
- **55 carded components: 47 with authored+graded previews, 8 honest floor cards.** ActivityPanel is bundled but uncarded (`componentSrcMap: null`) — its floor render throws "No QueryClient". All 56 barrel exports remain importable from `window.Tripl`.
- Build+validate exit 0; render check 55/55 clean. Conventions header (`conventions.md`) stitched into README. Anchor `_ds_sync.json` uploaded last. Project fully synced via the incremental plan.
- **8 floor cards** (need react-query/react-router context + live data; can't render statically in a static bundle): AppSidebar, TopBar, BranchSwitcher, EventPhotosSection, SettingsLayout, TopMoversPanel, ReleaseRegressionPanel, SeasonalityHeatmap. They have real `dtsPropsFor` contracts so the design agent can still use them inside an app shell.

### Authoring patterns (folded from wave learnings — learnings/ deleted)
- Import previews from `'frontend'` → resolves to `window.Tripl`. lucide-react / other node_modules libs CAN be imported directly in previews.
- Inline-style wrappers for layout glue; component-owned classes resolve from bundle CSS, preview-only utility classes do NOT (Tailwind only emits used classes; static stylesheet, no JIT — this is why conventions.md leads with `var(--*)` tokens).
- Overlays render in capture with `defaultOpen`/`open` (Radix portals to body). Tooltip needs `TooltipProvider`. ConfirmDialog → `cfg.overrides` cardMode single.
- Container-less rows (Field/InfoRow/Toggle/TextInput…) need an inline `var(--surface)`/`var(--border)` host box. Give cards/inputs explicit widths.
- Charts take inline mock time-series; chart metric types are NOT re-exported (inline the shapes). fg token names: `--fg/--fg-muted/--fg-subtle/--fg-faint` (NO `--fg-default`).

### Floor-card RESCUE paths (for a future re-sync, if desired)
- The 8 data composites need a bundle-provided provider so context identity matches: author a small wrapper exported from `frontend/.ds-entry.tsx` that wraps children in `QueryClientProvider` + `MemoryRouter` using the BUNDLE's own react-query/react-router, set `cfg.provider` to it. Even then most show loading/empty without a backend → likely still floor cards. SettingsLayout additionally needs `AuthContext` exported from the barrel + a mock auth value. Verdict: low ROI; floor cards are the honest baseline.

## RE-SYNC LOG — 2026-07-16 (no source changes; pipeline churn only)
- All 55 sourceKeys unchanged despite ~50 frontend commits since 2026-06-20 (none touched mapped component sources). Verdict: 0 changed/added/removed; uploaded 48 churned components + bundle/styling/docs (new converter version → scriptsSha/artifact churn).
- **New converter check `[GRID_OVERFLOW]`** flagged 29 components → `cfg.overrides` now sets `cardMode: "single"` (7 portal/overlay: AlertDialog, Dialog, DropdownMenu, Popover, Sheet, Tooltip, VariableValueContextTrigger w/ primaryStory OpenPanel) and `cardMode: "column"` (22 wide ones). Presentation-only; grades carried.
- **Playwright chromium cache (~/.cache/ms-playwright) had been wiped** — reinstall via `.ds-sync/node_modules/.bin/playwright install chromium` (staged playwright 1.61.0 pins build 1228). Expect this on re-sync; treat as routine setup, not a new decision.
- Spot-checks (EmptyState, Toaster, ScrollArea, Panel, Textarea + re-graded ConfirmDialog) all confirmed good. Render check 55/55 clean, 0 warns remaining.
- conventions.md: dropped the brittle "(239 tokens)" count (fresh build counts differ); all other names still verify.
- The project now also contains a user/app-created `mockups/` dir (13 files) — NOT sync-managed; never delete it in reconciliation.
- Known render warns: none.

## Re-sync risks (forward-looking)
- `frontend/.ds-styles.css` is gitignored & regenerated — re-sync MUST re-run cfg.buildCmd first.
- The two tsconfig gotchas are converter-lib behavior; if the bundled lib is updated, re-verify the alias plugin still needs the comment-free tsconfig.
- App composites are tied to live app code (contexts/api); their previews may break when that code changes.
- Node engine mismatch against the pinned version — harmless so far; watch on re-clone.

- 2026-09-25: Sheet, ScrollArea and ThemeToggle were deleted from the app in a7b11905 (#229); removed from the entry barrel, componentSrcMap, dtsPropsFor, overrides and previews. 52 carded components (ActivityPanel stays uncarded).
- 2026-09-25 re-sync (first since the initial import): 44 changed components, the shared bundle, styles, fonts and vendor files written; the three removed components' 15 files deleted; _ds_sync.json written last. Render check before the push: 52/52 clean (44 authored, 8 floor cards). list_files output can come back truncated — confirm a path with get_file before concluding it is missing.
- 2026-10-06 re-sync: 0 changed/added/removed (52 unchanged sourceKeys); the shared bundle, styling and every component's files re-uploaded because the dependency upgrades (React 19.3, lucide, recharts…) moved the compiled bundle. Canary spot-check (EmptyState, SCard, VariableValueContextTrigger) confirmed good. Build first failed on two new directory imports through `@/` (`@/components/states`, `@/extensions`) — same converter behaviour as gotcha #2; fixed with exact mappings in `frontend/tsconfig.dssync.json`, plus `@tripl/extensions` → `src/extensions/none.ts` (the Vite alias the converter does not see). A new `@/<dir>` barrel import will need the same line.
