// Project lint rules, loaded by Oxlint as a JS plugin (.oxlintrc.json,
// "jsPlugins"). Which files each rule covers is set there, in "overrides";
// the rules themselves apply to whatever file they run on. Tests:
// tripl.test.js next to this file (`node --test oxlint-plugins/*.test.js`).
//
// JS plugins are alpha in Oxlint 1.x: the API follows ESLint's (meta, create,
// context.report) and the AST is ESTree, so these rules would run unchanged
// under ESLint too.

// Every code-split component goes through lib/lazyWithReload.ts, which
// recovers a tab left open across a deploy. A bare React.lazy turns that
// first click after a release into a raw chunk-load error (#194).
// Checked on the AST rather than with an import ban, which would also reject
// every `import * as React from 'react'` because a namespace could reach lazy.
const noBareLazy = {
  meta: {
    type: 'problem',
    docs: { description: 'Use lazyWithReload instead of React.lazy.' },
    messages: {
      bareLazy: 'Use lazyWithReload from @/lib/lazyWithReload instead of React.lazy.',
    },
    schema: [],
  },
  create(context) {
    return {
      ImportDeclaration(node) {
        if (node.source.value !== 'react') return
        for (const specifier of node.specifiers) {
          if (specifier.type === 'ImportSpecifier' && specifier.imported.name === 'lazy') {
            context.report({ node: specifier, messageId: 'bareLazy' })
          }
        }
      },
      MemberExpression(node) {
        if (node.object.type === 'Identifier' && node.object.name === 'React' && node.property.name === 'lazy') {
          context.report({ node, messageId: 'bareLazy' })
        }
      },
    }
  },
}

// A query key typed out by hand is how one cache ends up with two spellings:
// the reader and the writer stop seeing each other and the screen goes quietly
// stale, or a key changes shape and an invalidation prefix stops matching.
// Keys are built in lib/queryKeys.ts; tests may still spell a key
// out, which is how they pin the value a builder produces.

// The QueryClient methods whose first argument is a bare key.
const KEY_FIRST_METHODS = /^(getQueryData|setQueryData|getQueryState|ensureQueryData|fetchQuery|prefetchQuery)$/

const noQueryKeyLiterals = {
  meta: {
    type: 'problem',
    docs: { description: 'Build query keys with a builder from lib/queryKeys.' },
    messages: {
      literalKey: 'Build query keys with a builder from @/lib/queryKeys instead of an array literal.',
    },
    schema: [],
  },
  create(context) {
    const report = (node) => context.report({ node, messageId: 'literalKey' })
    return {
      // `queryKey: [...]` and `queryKey: [...] as const`.
      Property(node) {
        if (node.key.name !== 'queryKey') return
        if (node.value.type === 'ArrayExpression') report(node.value)
        else if (node.value.type === 'TSAsExpression' && node.value.expression.type === 'ArrayExpression') {
          report(node.value.expression)
        }
      },
      // `<Component queryKey={[...]} />`.
      JSXAttribute(node) {
        if (node.name.name !== 'queryKey' || node.value?.type !== 'JSXExpressionContainer') return
        if (node.value.expression.type === 'ArrayExpression') report(node.value.expression)
      },
      // `queryClient.getQueryData([...])` and the other key-first methods.
      CallExpression(node) {
        const name = node.callee.type === 'MemberExpression' ? node.callee.property.name : undefined
        if (typeof name !== 'string' || !KEY_FIRST_METHODS.test(name)) return
        if (node.arguments[0]?.type === 'ArrayExpression') report(node.arguments[0])
      },
    }
  },
}

// Pages build selects from the kit's NativeSelect, not a raw <select> that
// copies the control styling by hand and drifts from it: two form-control
// systems with different sizes, borders and disabled states is how DS-9 began.
const noRawSelect = {
  meta: {
    type: 'suggestion',
    docs: { description: 'Use NativeSelect instead of a raw <select> in pages.' },
    messages: {
      rawSelect: 'Use NativeSelect from @/components/settings/kit instead of a raw <select>.',
    },
    schema: [],
  },
  create(context) {
    return {
      JSXOpeningElement(node) {
        if (node.name.type === 'JSXIdentifier' && node.name.name === 'select') {
          context.report({ node, messageId: 'rawSelect' })
        }
      },
    }
  },
}

// The type, icon and radius scales (index.css @theme):
// sizes come from the named steps, not a pixel value typed at the call site.
// That is how the app drifted to seven text sizes half a pixel apart. Tests
// may still spell a banned class, which is how they assert it is gone.
const ARBITRARY_SIZES = [
  { pattern: /(^|[\s:'"`])text-\[\d/, messageId: 'textSize' },
  { pattern: /(^|[\s:'"`])(size-\[(1\d|20)px\]|h-\[(1\d|20)px\] w-\[(1\d|20)px\])/, messageId: 'iconSize' },
  { pattern: /(^|[\s:'"`])rounded(-[a-z]+)?-\[\d/, messageId: 'radius' },
]

const noArbitrarySizes = {
  meta: {
    type: 'suggestion',
    docs: { description: 'Use the named text, icon and radius scales instead of arbitrary pixel values.' },
    messages: {
      textSize:
        'Use a named text size (text-micro, caption, body-sm, body, heading, title, display) instead of text-[Npx].',
      iconSize: 'Use the icon scale (size-3, size-3.5, size-4, size-5) instead of an arbitrary pixel size.',
      radius: 'Use rounded-sm, rounded-control or rounded-card instead of rounded-[Npx].',
    },
    schema: [],
  },
  create(context) {
    // One report per scale a string breaks, the way the ESLint selectors did.
    const check = (node, text) => {
      for (const { pattern, messageId } of ARBITRARY_SIZES) {
        if (pattern.test(text)) context.report({ node, messageId })
      }
    }
    return {
      Literal(node) {
        if (typeof node.value === 'string') check(node, node.value)
      },
      TemplateElement(node) {
        check(node, node.value.raw)
      },
    }
  },
}

// shadcn's `muted-foreground` is the tertiary grey but reads as "secondary",
// which is how body copy and captions ended up one colour. The alias
// is gone from index.css, so a class or var() naming it would also render
// with no colour at all. `sidebar-muted-foreground` is a different token.
const MUTED_FOREGROUND = /(?<!sidebar-)muted-foreground\b/

const noMutedForeground = {
  meta: {
    type: 'problem',
    docs: { description: 'Use the explicit fg steps instead of muted-foreground.' },
    messages: {
      mutedForeground:
        'muted-foreground no longer exists: use text-fg-tertiary for captions and meta, text-fg-secondary for body copy (var(--fg-tertiary) / var(--fg-secondary) in CSS values).',
    },
    schema: [],
  },
  create(context) {
    const check = (node, text) => {
      if (MUTED_FOREGROUND.test(text)) context.report({ node, messageId: 'mutedForeground' })
    }
    return {
      Literal(node) {
        if (typeof node.value === 'string') check(node, node.value)
      },
      TemplateElement(node) {
        check(node, node.value.raw)
      },
    }
  },
}

export default {
  meta: { name: 'tripl' },
  rules: {
    'no-bare-lazy': noBareLazy,
    'no-query-key-literals': noQueryKeyLiterals,
    'no-raw-select': noRawSelect,
    'no-arbitrary-sizes': noArbitrarySizes,
    'no-muted-foreground': noMutedForeground,
  },
}
