import { describe, expect, it } from 'vitest'
import type { DocLinkResolution } from '@/types/docs'
import {
  canonicalUuid,
  describeSuggestions,
  describeUnresolved,
  docLinkHref,
  docLinkRoute,
  docLinkSyntax,
  extractDocLinks,
  indexResolutions,
  isUnavailableNote,
  maskCode,
  parseDocLinkHref,
  relinkDocLinks,
  relinkWritten,
  remarkDocLinks,
  resolutionKey,
  resolveRelativeDocHref,
  suggestionLabel,
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
    expect(extractDocLinks('[[widget:x]] [[event:]] [[event:a\nb]]')).toEqual([])
  })

  it('reads the F24 kinds, by name and by id', () => {
    const noteId = '0f8fad5b-d9cb-469f-a165-70867728950e'
    const ruleId = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
    const userId = '16fd2706-8baf-433b-82eb-8c7fada847da'
    const links = extractDocLinks(
      [
        `[[doc:${noteId}#set-up|the setup]]`,
        '[[variable:country]] [[metric:signup_rate]] [[branch:feature_x]]',
        '[[scan:nightly]] [[data-source:warehouse]]',
        `[[alert-rule:${ruleId}]] ping [[user:${userId}]]`,
        '[[doc:guides/setup.md]]',
      ].join('\n'),
    )
    expect(links.map(l => [l.kind, l.target, l.anchor, l.label, l.ref])).toEqual([
      ['doc', noteId, 'set-up', 'the setup', `doc:${noteId}`],
      ['variable', 'country', null, null, 'variable:country'],
      ['metric', 'signup_rate', null, null, 'metric:signup_rate'],
      ['branch', 'feature_x', null, null, 'branch:feature_x'],
      ['scan', 'nightly', null, null, 'scan:nightly'],
      ['data_source', 'warehouse', null, null, 'data-source:warehouse'],
      ['alert_rule', ruleId, null, null, `alert-rule:${ruleId}`],
      ['user', userId, null, null, `user:${userId}`],
      ['doc', 'guides/setup.md', null, null, 'doc:guides/setup.md'],
    ])
  })

  it('ignores the new kinds inside code too', () => {
    expect(extractDocLinks('`[[metric:a]]`\n```\n[[user:b]]\n```\n[[scan:c]]').map(l => l.ref)).toEqual([
      'scan:c',
    ])
  })

  it('treats two anchors of one note as one link to resolve', () => {
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    expect(extractDocLinks(`[[doc:${id}#a]] and [[doc:${id.toUpperCase()}#b]]`)).toHaveLength(1)
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
      anchor: null,
      written: 'checkout/amount',
      label: null,
    })
  })

  it('carries an explicit label and a note anchor', () => {
    const href = docLinkHref('doc', 'n-1#install', 'Install | steps')
    expect(parseDocLinkHref(href)).toEqual({
      kind: 'doc',
      target: 'n-1',
      qualifier: null,
      anchor: 'install',
      written: 'n-1#install',
      label: 'Install | steps',
    })
    expect(parseDocLinkHref(docLinkHref('alert-rule', 'r-1'))?.kind).toBe('alert_rule')
    expect(parseDocLinkHref(docLinkHref('data-source', 'dw'))?.kind).toBe('data_source')
  })

  it('maps the event-type spelling to the API kind', () => {
    expect(parseDocLinkHref(docLinkHref('event-type', 'checkout'))?.kind).toBe('event_type')
  })

  it('rejects anything that is not a doc link', () => {
    expect(parseDocLinkHref('https://example.com')).toBeNull()
    expect(parseDocLinkHref('tripl:widget/x')).toBeNull()
    expect(parseDocLinkHref(undefined)).toBeNull()
  })

  it('writes the syntax for a Notes card pre-fill', () => {
    expect(docLinkSyntax('event_type', 'checkout')).toBe('[[event-type:checkout]]')
    expect(docLinkSyntax('field', 'amount', 'checkout')).toBe('[[field:checkout/amount]]')
    expect(docLinkSyntax('data_source', 'warehouse')).toBe('[[data-source:warehouse]]')
    expect(docLinkSyntax('metric', 'signup_rate')).toBe('[[metric:signup_rate]]')
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

  it('say where a broken plan link was looked up and list suggestions apart', () => {
    const broken = resolution({
      kind: 'metric',
      target: 'signup_rte',
      raw: '[[metric:signup_rte]]',
      status: 'broken',
      route_path: null,
      suggestions: ['signup_rate', 'signup_count'],
    })
    expect(describeUnresolved(broken)).toBe("[[metric:signup_rte]]: no metric named 'signup_rte' in the metric catalog.")
    expect(describeSuggestions(broken)).toBe('Did you mean: signup_rate, signup_count?')
    expect(describeSuggestions(resolution({}))).toBeNull()
  })

  it('never name a note the reader cannot see', () => {
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    // Hidden and deleted notes come back the same: unavailable, no reason.
    const hidden = resolution({ kind: 'doc', target: id, raw: `[[doc:${id}]]`, status: 'unavailable', route_path: null })
    expect(isUnavailableNote(hidden)).toBe(true)
    expect(describeUnresolved(hidden)).not.toContain(id)
    // A hand-typed path is the author's own text: it may be repeated.
    const typed = resolution({ kind: 'doc', target: 'x.md', raw: '[[doc:x.md]]', status: 'broken', route_path: null })
    expect(isUnavailableNote(typed)).toBe(false)
    expect(describeUnresolved(typed)).toContain('x.md')
  })

  it('offer a typed note path by the title of the readable note there', () => {
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    const typed = resolution({
      kind: 'doc',
      target: 'guides/setup.md',
      raw: '[[doc:guides/setup.md]]',
      status: 'broken',
      route_path: null,
      reason: 'path_form',
      label: 'Setup guide',
      suggestions: [id],
    })
    expect(describeUnresolved(typed)).toBe(
      "[[doc:guides/setup.md]]: notes are linked by id. Save the note to link 'Setup guide' by its id.",
    )
    expect(describeUnresolved(typed)).not.toContain('that you can read')
    expect(suggestionLabel(typed, id)).toBe('Setup guide')
    expect(describeSuggestions(typed)).toBe('Did you mean: Setup guide?')
    // A by-name suggestion reads as itself.
    expect(suggestionLabel(resolution({ kind: 'metric', suggestions: ['signup_rate'] }), 'signup_rate')).toBe('signup_rate')
  })

  it('canonicalise every id spelling the server accepts', () => {
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    for (const spelling of [
      id,
      id.toUpperCase(),
      id.replaceAll('-', ''),
      `{${id}}`,
      `urn:uuid:${id}`,
      ` ${id.replaceAll('-', '').toUpperCase()} `,
    ]) {
      expect(canonicalUuid(spelling)).toBe(id)
    }
    expect(canonicalUuid('guides/setup.md')).toBeNull()
    expect(canonicalUuid(`${id}0`)).toBeNull()
    // The key of a link written unhyphenated matches the server's resolution.
    const index = indexResolutions([resolution({ kind: 'alert_rule', target: id })])
    expect(index.get(resolutionKey('alert_rule', `{${id.replaceAll('-', '')}}`, null))).toBeDefined()
  })

  it('open a note link at its own anchor, never another link to the same note', () => {
    expect(docLinkRoute('/p/demo/docs/project/a.md#first', 'second')).toBe('/p/demo/docs/project/a.md#second')
    expect(docLinkRoute('/p/demo/docs/project/a.md#first', null)).toBe('/p/demo/docs/project/a.md')
    expect(docLinkRoute('/p/demo/docs/project/a.md', 'set up')).toBe('/p/demo/docs/project/a.md#set%20up')
  })

  it('index a note by id whatever its anchor or case', () => {
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    const index = indexResolutions([resolution({ kind: 'doc', target: id, qualifier: null })])
    expect(index.get(resolutionKey('doc', `${id.toUpperCase()}#top`, null))).toBeDefined()
  })
})

describe('relink', () => {
  it('re-points every occurrence outside code and keeps each label', () => {
    const md = '[[metric:old]] and [[metric:old|the rate]] but `[[metric:old]]` stays; [[event:old]] too'
    const next = relinkDocLinks(md, { kind: 'metric', target: 'old', qualifier: null }, 'signup_rate')
    expect(next).toBe(
      '[[metric:signup_rate]] and [[metric:signup_rate|the rate]] but `[[metric:old]]` stays; [[event:old]] too',
    )
  })

  it('keeps a field qualified by its event type', () => {
    expect(relinkWritten({ kind: 'field', qualifier: 'checkout' }, 'amount')).toBe('checkout/amount')
    expect(relinkWritten({ kind: 'field', qualifier: 'checkout' }, 'cart/amount')).toBe('cart/amount')
    expect(relinkWritten({ kind: 'variable', qualifier: null }, 'country')).toBe('country')
    const md = '[[field:checkout/amt]]'
    expect(relinkDocLinks(md, { kind: 'field', target: 'amt', qualifier: 'Checkout' }, 'checkout/amount')).toBe(
      '[[field:checkout/amount]]',
    )
  })

  it('keeps each note link anchor when a typed path is relinked to an id', () => {
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    const md = '[[doc:guides/setup.md#install|Install]] and [[doc:guides/setup.md]]'
    expect(relinkDocLinks(md, { kind: 'doc', target: 'guides/setup.md', qualifier: null }, id)).toBe(
      `[[doc:${id}#install|Install]] and [[doc:${id}]]`,
    )
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
        { type: 'link', url: 'tripl:event/a?l=A', title: null, children: [{ type: 'text', value: 'A' }] },
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
