import { describe, expect, it } from 'vitest'
import { applyPick, findLinkTrigger, pickReplacement, triggerInCode } from './docLinkTrigger'

describe('findLinkTrigger (F24)', () => {
  it('opens on [[ for every kind', () => {
    expect(findLinkTrigger('See [[', 10)).toEqual({ mode: 'link', from: 14, to: 16, kind: null, query: '' })
    expect(findLinkTrigger('See [[sign', 0)).toEqual({ mode: 'link', from: 4, to: 10, kind: null, query: 'sign' })
  })

  it('narrows to the kind typed before the colon', () => {
    expect(findLinkTrigger('[[metric:', 0)).toMatchObject({ kind: 'metric', query: '' })
    expect(findLinkTrigger('[[metric:sig', 0)).toMatchObject({ kind: 'metric', query: 'sig' })
    expect(findLinkTrigger('[[event-type:chk', 0)).toMatchObject({ kind: 'event_type', query: 'chk' })
    expect(findLinkTrigger('[[alert-rule:', 0)).toMatchObject({ kind: 'alert_rule' })
    expect(findLinkTrigger('[[data-source:dw', 0)).toMatchObject({ kind: 'data_source', query: 'dw' })
    expect(findLinkTrigger('[[doc:guides/', 0)).toMatchObject({ kind: 'doc', query: 'guides/' })
    expect(findLinkTrigger('[[user:', 0)).toMatchObject({ kind: 'user' })
  })

  it('keeps an unknown prefix as part of the query', () => {
    expect(findLinkTrigger('[[widget:x', 0)).toMatchObject({ kind: null, query: 'widget:x' })
  })

  it('does not open on a closed link, a single bracket or a label', () => {
    expect(findLinkTrigger('[[metric:x]]', 0)).toBeNull()
    expect(findLinkTrigger('[x', 0)).toBeNull()
    expect(findLinkTrigger('[[metric:x|the', 0)).toBeNull()
  })

  it('opens the people picker on @ at a word boundary only', () => {
    expect(findLinkTrigger('@', 5)).toEqual({ mode: 'mention', from: 5, to: 6, kind: 'user', query: '' })
    expect(findLinkTrigger('Thanks @ad', 0)).toEqual({ mode: 'mention', from: 7, to: 10, kind: 'user', query: 'ad' })
    expect(findLinkTrigger('(@ad', 0)).toMatchObject({ mode: 'mention', from: 1 })
    expect(findLinkTrigger('mail a@example', 0)).toBeNull()
    expect(findLinkTrigger('Thanks @ad ', 0)).toBeNull()
  })
})

describe('triggerInCode', () => {
  it('knows a trigger inside inline or fenced code', () => {
    const inline = 'Use `x` then `[[met'
    // An unclosed code span is not code yet: the picker may open.
    expect(triggerInCode(inline, { from: inline.indexOf('[[') })).toBe(false)
    const fenced = '```\n[[met'
    expect(triggerInCode(fenced, { from: fenced.indexOf('[[') })).toBe(true)
    const prose = 'text [[met'
    expect(triggerInCode(prose, { from: prose.indexOf('[[') })).toBe(false)
  })
})

describe('picking', () => {
  it('swallows an auto-closed ]] after the cursor', () => {
    expect(pickReplacement('link', ']] more')).toEqual({ extra: 2 })
    expect(pickReplacement('link', '] more')).toEqual({ extra: 1 })
    expect(pickReplacement('link', ' more')).toEqual({ extra: 0 })
    expect(pickReplacement('mention', ']] more')).toEqual({ extra: 0 })
  })

  it('splices the reference over the trigger', () => {
    const text = 'See [[met]] now'
    expect(applyPick(text, { mode: 'link', from: 4, to: 9, kind: null, query: 'met' }, '[[metric:signup_rate]]')).toEqual({
      text: 'See [[metric:signup_rate]] now',
      cursor: 26,
    })
    expect(applyPick('Hi @ad', { mode: 'mention', from: 3, to: 6, kind: 'user', query: 'ad' }, '[[user:u-1]]').text).toBe(
      'Hi [[user:u-1]]',
    )
  })
})
