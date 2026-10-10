// Tests for the project lint rules in tripl.js. `bun run lint` runs them with
// Node's test runner (Oxlint's RuleTester refuses any other runtime) (`node --test oxlint-plugins/*.test.js`); Oxlint's
// RuleTester parses each case with the same parser the linter uses. Which
// files a rule covers is .oxlintrc.json's business and is not tested here.
import { describe, it } from 'node:test'
import { RuleTester } from 'oxlint/plugins-dev'
import plugin from './tripl.js'

RuleTester.describe = describe
RuleTester.it = it

const tester = new RuleTester({
  languageOptions: { sourceType: 'module', parserOptions: { lang: 'tsx' } },
})

tester.run('no-bare-lazy', plugin.rules['no-bare-lazy'], {
  valid: [
    "import { lazyWithReload } from '@/lib/lazyWithReload'",
    "import * as React from 'react'\nReact.useState(0)",
    "import { lazy } from 'somewhere-else'",
  ],
  invalid: [
    { code: "import { lazy } from 'react'", errors: [{ messageId: 'bareLazy' }] },
    { code: "import { lazy as l, useState } from 'react'", errors: [{ messageId: 'bareLazy' }] },
    {
      code: "import * as React from 'react'\nconst Page = React.lazy(() => import('./Page'))",
      errors: [{ messageId: 'bareLazy' }],
    },
  ],
})

tester.run('no-query-key-literals', plugin.rules['no-query-key-literals'], {
  valid: [
    'useQuery({ queryKey: queryKeys.events(projectId), queryFn })',
    'queryClient.getQueryData(queryKeys.me())',
    'queryClient.invalidateQueries({ queryKey: queryKeys.events.all })',
    // Not a key-first method: its argument is a filter object.
    "queryClient.removeQueries(['x'])",
    '<Refresh queryKey={queryKeys.me()} />',
  ],
  invalid: [
    { code: "useQuery({ queryKey: ['events', id] })", errors: [{ messageId: 'literalKey' }] },
    { code: "useQuery({ queryKey: ['events'] as const })", errors: [{ messageId: 'literalKey' }] },
    { code: "<Refresh queryKey={['events']} />", errors: [{ messageId: 'literalKey' }] },
    { code: "queryClient.setQueryData(['me'], user)", errors: [{ messageId: 'literalKey' }] },
    { code: "qc.prefetchQuery(['a'])", errors: [{ messageId: 'literalKey' }] },
  ],
})

tester.run('no-raw-select', plugin.rules['no-raw-select'], {
  valid: ['<NativeSelect value={v} onChange={f} />', '<Select value={v} />'],
  invalid: [
    { code: '<select value={v} onChange={f}><option /></select>', errors: [{ messageId: 'rawSelect' }] },
  ],
})

tester.run('no-arbitrary-sizes', plugin.rules['no-arbitrary-sizes'], {
  valid: [
    '<div className="text-caption size-4 rounded-control" />',
    // Not a size: an arbitrary colour, and a class merely containing the prefix.
    '<div className="text-[var(--fg)] context-[1]" />',
    'const icon = `size-[24px]`',
  ],
  invalid: [
    { code: '<div className="text-[11px]" />', errors: [{ messageId: 'textSize' }] },
    { code: 'cn("p-1", "md:text-[13px]")', errors: [{ messageId: 'textSize' }] },
    { code: 'const c = `gap-1 size-[14px]`', errors: [{ messageId: 'iconSize' }] },
    { code: '<svg className="h-[16px] w-[16px]" />', errors: [{ messageId: 'iconSize' }] },
    { code: '<div className="rounded-t-[6px]" />', errors: [{ messageId: 'radius' }] },
    {
      code: '<div className="text-[12px] rounded-[4px]" />',
      errors: [{ messageId: 'textSize' }, { messageId: 'radius' }],
    },
  ],
})

tester.run('no-muted-foreground', plugin.rules['no-muted-foreground'], {
  valid: [
    '<p className="text-fg-tertiary" />',
    "const color = 'var(--fg-tertiary)'",
    // A different token that happens to share the suffix.
    '<span className="text-sidebar-muted-foreground" />',
  ],
  invalid: [
    { code: '<p className="text-caption text-muted-foreground" />', errors: [{ messageId: 'mutedForeground' }] },
    { code: 'cn("placeholder:text-muted-foreground")', errors: [{ messageId: 'mutedForeground' }] },
    { code: 'const c = `fill-muted-foreground ${x}`', errors: [{ messageId: 'mutedForeground' }] },
    { code: "const color = 'var(--muted-foreground)'", errors: [{ messageId: 'mutedForeground' }] },
  ],
})

tester.run('no-bare-locale', plugin.rules['no-bare-locale'], {
  valid: [
    'count.toLocaleString(APP_LOCALE)',
    "date.toLocaleDateString(APP_LOCALE, { month: 'short' })",
    "date.toLocaleTimeString('en-US', { hour: 'numeric' })",
    'formatNumber(count)',
    'value.toString()',
    // Reading the browser's zone is not formatting in its locale.
    'Intl.DateTimeFormat().resolvedOptions().timeZone',
  ],
  invalid: [
    { code: 'total.toLocaleString()', errors: [{ messageId: 'bareLocale' }] },
    { code: '`${(n - 1).toLocaleString()} more`', errors: [{ messageId: 'bareLocale' }] },
    { code: 'row.count?.toLocaleString()', errors: [{ messageId: 'bareLocale' }] },
    {
      code: "day.toLocaleDateString(undefined, { weekday: 'long' })",
      errors: [{ messageId: 'bareLocale' }],
    },
    { code: 'at.toLocaleTimeString()', errors: [{ messageId: 'bareLocale' }] },
  ],
})
