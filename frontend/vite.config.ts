import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { compression } from 'vite-plugin-compression2'
import path from 'path'
import zlib from 'node:zlib'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    // Pre-compress JS/CSS/SVG/JSON to .br and .gz files served via nginx
    // brotli_static / gzip_static. Both: browsers only offer brotli over HTTPS,
    // so a plain-HTTP self-hosted install is served the gzip copy.
    compression({
      // [algorithm, options] — build-time, so max quality is fine.
      algorithms: [
        [
          'brotliCompress',
          {
            params: {
              [zlib.constants.BROTLI_PARAM_QUALITY]: 11,
            },
          },
        ],
        ['gzip', { level: 9 }],
      ],
      include: [/\.(js|mjs|css|html|svg|json|wasm)$/],
      threshold: 1024,
      deleteOriginalAssets: false,
    }),
  ],
  resolve: {
    alias: {
      // Frontend extensions beyond the bundled one (src/extensions): none
      // unless the build names a module whose default export lists them.
      '@tripl/extensions': process.env.TRIPL_EXTENSIONS_ENTRY
        ? path.resolve(process.env.TRIPL_EXTENSIONS_ENTRY)
        : path.resolve(import.meta.dirname, './src/extensions/none.ts'),
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Split vendor chunks so that only the pages drawing charts pay for
    // recharts, and so upgrading a single dep doesn't bust the whole cache.
    //
    // Explicit rolldown groups, not `manualChunks`: rolldown turns a
    // `manualChunks` function into groups that also pull in every captured
    // module's dependencies, first match wins. The recharts rule therefore
    // claimed React, clsx and the rest of what recharts shares with the shell,
    // every chunk imported React from `charts-vendor`, and index.html
    // modulepreloaded all of recharts on every page, /auth included (#194).
    // Priorities make the shell's own libraries win: React first, then the
    // other named vendors, then ANY dependency on the initial graph, and only
    // what is left over — recharts and the d3/redux code nothing else uses —
    // lands in `charts-vendor`. scripts/check-bundle-budget.mjs fails the build
    // if that chunk returns to the critical path.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: 'react-vendor',
              test: /node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/,
              priority: 40,
            },
            { name: 'tanstack-vendor', test: /node_modules[\\/]@tanstack[\\/]/, priority: 30 },
            { name: 'radix-vendor', test: /node_modules[\\/]@radix-ui[\\/]/, priority: 20 },
            // Everything else the first render already needs (clsx, lucide,
            // sonner…). Tagged `$initial`, so nothing only a lazy page imports
            // is dragged onto the critical path through this group.
            { name: 'vendor', test: /node_modules[\\/]/, tags: ['$initial'], priority: 10 },
            { name: 'charts-vendor', test: /node_modules[\\/]recharts[\\/]/, priority: 1 },
          ],
        },
      },
    },
  },
  test: {
    globals: true,
    setupFiles: './src/test-setup.ts',
    // Spies made with vi.spyOn are put back before every test, so one that a
    // failing test never restored cannot leak into the next.
    restoreMocks: true,
    // Pure-logic `*.test.ts` files run in node; paying for a jsdom environment
    // they never touch used to cost more than the tests themselves. Component
    // tests (`*.test.tsx`) run in jsdom. A `.ts` file that needs a DOM (a hook
    // test through renderHook, say) opts in with `// @vitest-environment jsdom`
    // on its first line.
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          environment: 'node',
          include: ['src/**/*.test.ts', 'tests/**/*.test.ts'],
        },
      },
      {
        extends: true,
        test: {
          name: 'jsdom',
          environment: 'jsdom',
          include: ['src/**/*.test.tsx'],
        },
      },
    ],
    // Vitest's 5000ms default caps the whole test, including time Testing Library
    // spends inside `findBy*`/`waitFor` — so it has to stay comfortably above the
    // 5000ms `asyncUtilTimeout` set in test-setup.ts, or a slow wait dies as an
    // unhelpful "Test timed out" instead of surfacing what the DOM actually held.
    // Headroom for a loaded machine, not licence for genuinely slow tests.
    testTimeout: 15000,
    // Vitest defaults to roughly one worker per core. On a small dev box that
    // exhausts memory and surfaces as a bare "Test timed out" with no assertion
    // failure — a false red that reads like a real regression. Capped for local
    // runs, left at the default in CI where the runner is sized for it.
    //
    // This has to be declarative rather than a flag on the command: passing
    // `pnpm test -- --maxWorkers=2` silently did NOTHING (under pnpm, before
    // bun), because pnpm appended the flag after vitest's own `--` passthrough
    // separator and cac filed it into args['--'] without either applying or
    // rejecting it. The run then
    // looks capped while executing at full concurrency. Set
    // here, it cannot be bypassed by how the suite happens to be invoked.
    maxWorkers: process.env.CI ? undefined : 2,
    // `bun run test:coverage` (CI runs it in place of `bun run test`, same test set).
    // `include` is what makes a source file no test imports count as 0% rather
    // than vanish from the report. The thresholds are a ratchet: raise them to
    // the new measured value when coverage rises, never lower them to get a
    // red run through — write the missing test instead (CONTRIBUTING.md,
    // "Coverage").
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/*.d.ts',
        'src/test/**',
        'src/test-setup.ts',
        // Generated from backend/openapi.json: types only, nothing to execute.
        'src/types/api.gen.ts',
        // The entry point mounts the app into the real document; no test runs it.
        'src/main.tsx',
      ],
      // text-summary for the job log, json-summary for the CI artifact.
      reporter: ['text-summary', 'json-summary'],
      thresholds: {
        // Measured on the full suite on 2026-09-26, rounded down. A ratchet:
        // raise a floor when coverage rises, never lower one to pass a PR.
        lines: 91,
        statements: 89,
        functions: 84,
        branches: 85,
        // Per-directory floors, so a coverage drop in these
        // directories cannot hide behind the app-wide average. src/lib is pure
        // logic with no DOM to excuse a gap.
        'src/lib/**': {
          lines: 97,
          statements: 97,
          functions: 98,
          branches: 92,
        },
        'src/demo/**': {
          lines: 96,
          statements: 93,
          functions: 93,
          branches: 89,
        },
      },
    },
  },
})
