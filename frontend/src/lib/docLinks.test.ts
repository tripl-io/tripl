import { describe, expect, it } from 'vitest'
import type { DocLinkResolution } from '@/types/docs'
import {
  describeUnresolved,
  docLinkHref,
  docLinkSyntax,
  extractDocLinks,
  indexResolutions,
  maskCode,
  parseDocLinkHref,
  remarkDocLinks,
  resolutionKey,
  resolveRelativeDocHref,
} from './docLinks'

function resolution(overrides: Partial<DocLinkResolution>): DocLinkResolution {
  return {
    kind: 'event',
    target: 'checkout_started',
    qualifier: null,
    raw: '[[event:checkout_started]]',
    status: 'resolved',
    route_path: '/p/demo/monitoring/event/e-1',
    entity_id: 'e-1',
    candidates: 1,
    ...overrides,
  }
}

describe('extractDocLinks (F22)', () => {
  it('reads every kind, a label and a qualified field', () => {
    const links = extractDocLinks(
      'See [[event:checkout_started|the start]], [[event-type:checkout]] and [[field:checkout/amount]].',
    )
    expect(links.map(l => [l.kind, l.target, l.qualifier, l.label, l.ref])).toEqual([
      ['event', 'checkout_started', null, 'the start', 'event:checkout_started'],
      ['event_type', 'checkout', null, null, 'event-type:checkout'],
      ['field', 'amount', 'checkout', null, 'field:checkout/amount'],
    ])
  })

  it('ignores links in fenced and inline code', () => {
    const md = [
      'Real [[event:a]].',
      '```md',
      '[[event:in_fence]]',
      '```',
      'Inline `[[event:in_code]]` stays text.',
      '~~~',
      '[[event:in_tilde]]',
      '~~~',
    ].join('\n')
    expect(extractDocLinks(md).map(l => l.target)).toEqual(['a'])
  })

  it('drops duplicates and keeps the first spelling', () => {
    const links = extractDocLinks('[[event:x]] then [[event:x|again]] and [[field:x]]')
    expect(links.map(l => l.ref)).toEqual(['event:x', 'field:x'])
  })

  it('does not match an unknown kind or an empty name', () => {
    expect(extractDocLinks('[[metric:x]] [[event:]] [[event:a\nb]]')).toEqual([])
  })
})

describe('maskCode', () => {
  it('keeps offsets and newlines', () => {
    const md = 'a `code` b\n```\nx\n```\nc'
    const masked = maskCode(md)
    expect(masked).toHaveLength(md.length)
    expect(masked.split('\n')).toHaveLength(md.split('\n').length)
    expect(masked).toContain('a ')
    expect(masked).not.toContain('code')
  })
})

describe('link hrefs', () => {
  it('round-trips a qualified field', () => {
    const href = docLinkHref('field', 'checkout/amount')
    expect(href).toBe('tripl:field/checkout%2Famount')
    expect(parseDocLinkHref(href)).toEqual({
      kind: 'field',
      target: 'amount',
      qualifier: 'checkout',
      written: 'checkout/amount',
    })
  })

  it('maps the event-type spelling to the API kind', () => {
    expect(parseDocLinkHref(docLinkHref('event-type', 'checkout'))?.kind).toBe('event_type')
  })

  it('rejects anything that is not a doc link', () => {
    expect(parseDocLinkHref('https://example.com')).toBeNull()
    expect(parseDocLinkHref('tripl:metric/x')).toBeNull()
    expect(parseDocLinkHref(undefined)).toBeNull()
  })

  it('writes the syntax for a Notes card pre-fill', () => {
    expect(docLinkSyntax('event_type', 'checkout')).toBe('[[event-type:checkout]]')
    expect(docLinkSyntax('field', 'amount', 'checkout')).toBe('[[field:checkout/amount]]')
  })
})

describe('resolutions', () => {
  it('index by kind, qualifier and target', () => {
    const index = indexResolutions([resolution({}), resolution({ kind: 'field', target: 'amount', qualifier: 'Checkout' })])
    expect(index.get(resolutionKey('event', 'checkout_started', null))?.entity_id).toBe('e-1')
    // Qualifiers compare case-insensitively, as the server matches type names.
    expect(index.get(resolutionKey('field', 'amount', 'checkout'))).toBeDefined()
  })

  it('describe a broken and an ambiguous link', () => {
    expect(describeUnresolved(resolution({ status: 'broken', route_path: null, candidates: 0 }))).toBe(
      "[[event:checkout_started]]: no event named 'checkout_started' on the main plan.",
    )
    expect(
      describeUnresolved(resolution({ kind: 'event_type', target: 'x', raw: '[[event-type:x]]', status: 'ambiguous', candidates: 2 })),
    ).toContain('2 event types')
  })
})

describe('remarkDocLinks', () => {
  it('turns links in text into link nodes and leaves code alone', () => {
    const tree = {
      type: 'root',
      children: [
        { type: 'paragraph', children: [{ type: 'text', value: 'Go [[event:a|A]] now' }] },
        { type: 'code', value: '[[event:b]]' },
        { type: 'paragraph', children: [{ type: 'inlineCode', value: '[[event:c]]' }] },
      ],
    }
    remarkDocLinks()(tree)
    expect(tree.children[0]).toEqual({
      type: 'paragraph',
      children: [
        { type: 'text', value: 'Go ' },
        { type: 'link', url: 'tripl:event/a', title: null, children: [{ type: 'text', value: 'A' }] },
        { type: 'text', value: ' now' },
      ],
    })
    expect(tree.children[1]).toEqual({ type: 'code', value: '[[event:b]]' })
    expect(tree.children[2]).toEqual({ type: 'paragraph', children: [{ type: 'inlineCode', value: '[[event:c]]' }] })
  })
})

describe('resolveRelativeDocHref', () => {
  it('resolves siblings, parents and anchors', () => {
    expect(resolveRelativeDocHref('guides/setup.md', 'install.md')).toEqual({ path: 'guides/install.md', hash: '' })
    expect(resolveRelativeDocHref('guides/setup.md', './a/b.md#top')).toEqual({ path: 'guides/a/b.md', hash: '#top' })
    expect(resolveRelativeDocHref('guides/setup.md', '../README.md')).toEqual({ path: 'README.md', hash: '' })
    expect(resolveRelativeDocHref('SKILL.md', 'references/api%20notes.md')).toEqual({
      path: 'references/api notes.md',
      hash: '',
    })
  })

  it('leaves external, absolute, anchor-only and escaping links alone', () => {
    expect(resolveRelativeDocHref('a.md', 'https://example.com/x.md')).toBeNull()
    expect(resolveRelativeDocHref('a.md', '/x.md')).toBeNull()
    expect(resolveRelativeDocHref('a.md', '#section')).toBeNull()
    expect(resolveRelativeDocHref('a.md', '../x.md')).toBeNull()
    expect(resolveRelativeDocHref('a.md', 'image.png')).toBeNull()
  })
})
