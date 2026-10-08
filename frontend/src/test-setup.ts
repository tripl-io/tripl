import { format } from 'node:util'
import { afterEach, beforeEach, expect, vi } from 'vitest'
import type { TestingLibraryMatchers } from '@testing-library/jest-dom/matchers'
import type { AxeMatchers } from 'vitest-axe/matchers'
import { setCurrentOrgSlug } from '@/lib/activeOrg'

// This file runs for both projects in vite.config.ts: the `node` one (pure
// `*.test.ts` logic) and the `jsdom` one (components). The DOM half is loaded
// only where a DOM exists, so a pure-logic file does not pay for importing
// Testing Library, jest-dom and axe.
const hasDom = typeof window !== 'undefined' && typeof document !== 'undefined'

if (hasDom) {
  // Not '@testing-library/jest-dom/vitest': that entry's type augmentation is
  // written for vitest 4's `Assertion<T>` and cannot merge with vitest 5's
  // `Assertion<R, T>` (testing-library/jest-dom#738). Register the same
  // matchers here and declare their types below.
  // Its types say `export =`, but the ESM build has named exports only — the
  // same namespace jest-dom's own vitest entry passes to expect.extend.
  const jestDomMatchers = await import('@testing-library/jest-dom/matchers')
  expect.extend(jestDomMatchers as unknown as Parameters<typeof expect.extend>[0])
  const { configure } = await import('@testing-library/react')
  const axeMatchers = await import('vitest-axe/matchers')

  // vitest-axe@0.1.0 ships an empty `extend-expect` entry, so register the
  // axe matchers manually. This makes `expect(...).toHaveNoViolations()` work.
  expect.extend(axeMatchers)

  // Testing Library's 1000ms default for `findBy*`/`waitFor` is too tight for the
  // route-level tests: resolving a lazy route chunk and settling its first render
  // can exceed it whenever the machine is busy (a loaded CI runner, or a backend
  // suite competing for CPU locally), which shows up as flakes that never reproduce
  // in isolation. The headroom only costs wall-clock on an assertion that was going
  // to fail anyway.
  configure({ asyncUtilTimeout: 5000 })
}

// Neither vitest-axe (broken `extend-expect` runtime import) nor jest-dom
// (vitest 4 signature, see above) ships a usable augmentation, so declare both
// matcher sets against vitest's `Assertion` here. The type parameters must match
// vitest's own declaration exactly (TS2428): vitest 5 declares
// `Assertion<R extends void | Promise<void> = void, T = unknown>`.
declare module 'vitest' {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  interface Assertion<R extends void | Promise<void> = void, T = unknown> extends AxeMatchers, TestingLibraryMatchers<any, R> {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  interface AsymmetricMatchersContaining extends AxeMatchers, TestingLibraryMatchers<any, any> {}
}

// jsdom's `localStorage` varies by version — CI's build omits it entirely, so a
// test's `localStorage.clear()` throws, which aborts Testing Library's afterEach
// cleanup and leaks rendered DOM into later tests. Node ships its own Web Storage
// that warns without `--localstorage-file`. Install a fresh in-memory Storage
// unconditionally so every test file behaves identically everywhere.
function memoryStorage(): Storage {
  const store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    clear: () => store.clear(),
    getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    removeItem: (key: string) => {
      store.delete(key)
    },
    setItem: (key: string, value: string) => {
      store.set(key, String(value))
    },
  }
}
for (const name of ['localStorage', 'sessionStorage'] as const) {
  Object.defineProperty(globalThis, name, {
    value: memoryStorage(),
    configurable: true,
    writable: true,
  })
}

// jsdom has no matchMedia. Every query answers "no match" unless a test installs
// its own, so a component that reads `prefers-reduced-motion` or a breakpoint
// does not depend on its own feature guard to render under test. Reinstalled
// before every test because some suites delete or stub it.
function installMatchMedia() {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// Messages a test run is known to print and does not yet fail on. Each entry is
// debt with an owner: remove it once the named issue fixes the cause. Anything
// else printed through console.error / console.warn fails the test that printed
// it, because those warnings are how React reports invalid DOM nesting, state
// updates outside act(), and react-query reports a query that resolved to
// undefined — real defects that used to pass green.
const KNOWN_CONSOLE_NOISE: RegExp[] = []

const consoleCalls: string[] = []

beforeEach(() => {
  if (hasDom) installMatchMedia()
  for (const level of ['error', 'warn'] as const) {
    vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
      consoleCalls.push(`console.${level}: ${format(...args)}`)
    })
  }
})

afterEach(async () => {
  // Global state that used to leak from one test into the next: persisted
  // storage, fake timers left on by a test that failed before restoring them,
  // and stubbed globals.
  vi.unstubAllGlobals()
  // The active organization is module state (lib/activeOrg.ts); a test that
  // mounts the org provider, or sets it, must not act in it for the next.
  setCurrentOrgSlug(null)
  localStorage.clear()
  sessionStorage.clear()
  vi.useRealTimers()

  const unexpected = consoleCalls.filter(
    (message) => !KNOWN_CONSOLE_NOISE.some((pattern) => pattern.test(message)),
  )
  consoleCalls.length = 0
  if (unexpected.length > 0) {
    // Throwing here skips Testing Library's own afterEach cleanup, which left
    // this test's DOM mounted for the next one and turned one noisy test into
    // a cascade of "found multiple elements" failures. Unmount first.
    if (hasDom) {
      const { cleanup } = await import('@testing-library/react')
      cleanup()
    }
    throw new Error(
      `The test printed ${unexpected.length} unexpected console message(s). ` +
        'Fix the cause, or spy on console yourself if the message is the point of the test.\n\n' +
        unexpected.join('\n\n'),
    )
  }
})

if (hasDom && !window.ResizeObserver) {
  class ResizeObserverMock {
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  window.ResizeObserver = ResizeObserverMock as typeof ResizeObserver
  globalThis.ResizeObserver = ResizeObserverMock as typeof ResizeObserver
}

if (hasDom && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function () {}
}
