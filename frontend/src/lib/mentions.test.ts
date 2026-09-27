import { describe, expect, it } from 'vitest'
import {
  activeMentionQuery,
  filterMentionCandidates,
  formatMention,
  insertMention,
  mentionedUserIds,
  parseMentions,
} from './mentions'

const ADA = '11111111-2222-3333-4444-555555555555'
const BOB = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

describe('mentions', () => {
  it('formats a token the parser reads back', () => {
    const body = `hi ${formatMention('Ada Lovelace', ADA)}, see this`
    expect(parseMentions(body)).toEqual([
      { kind: 'text', text: 'hi ' },
      { kind: 'mention', name: 'Ada Lovelace', userId: ADA },
      { kind: 'text', text: ', see this' },
    ])
  })

  it('strips brackets from a name so the token stays parseable', () => {
    expect(formatMention('Ada [ops]', ADA)).toBe(`@[Ada ops](${ADA})`)
    expect(formatMention('  ', ADA)).toBe(`@[someone](${ADA})`)
  })

  it('leaves a plain @name and a malformed token as text', () => {
    expect(parseMentions('ping @ada and @[Bob](not-a-uuid)')).toEqual([
      { kind: 'text', text: 'ping @ada and @[Bob](not-a-uuid)' },
    ])
  })

  it('lists each mentioned user once', () => {
    const body = `${formatMention('Ada', ADA)} ${formatMention('Bob', BOB)} ${formatMention('Ada', ADA)}`
    expect(mentionedUserIds(body)).toEqual([ADA, BOB])
  })

  it('finds the @query at the caret only at a word start', () => {
    expect(activeMentionQuery('hello @ad', 9)).toEqual({ start: 6, query: 'ad' })
    expect(activeMentionQuery('@', 1)).toEqual({ start: 0, query: '' })
    expect(activeMentionQuery('mail@example', 12)).toBeNull()
    expect(activeMentionQuery('hello @ada done', 15)).toBeNull()
  })

  it('replaces the query with the token and moves the caret past it', () => {
    const result = insertMention('hey @ad!', 4, 7, 'Ada', ADA)
    expect(result.text).toBe(`hey @[Ada](${ADA}) !`)
    expect(result.caret).toBe(`hey @[Ada](${ADA}) `.length)
  })

  it('ranks name matches before email matches', () => {
    const candidates = [
      { userId: BOB, name: 'Bob Stone', email: 'ada.fan@example.com' },
      { userId: ADA, name: 'Ada Lovelace', email: 'al@example.com' },
    ]
    expect(filterMentionCandidates(candidates, 'ada').map(c => c.userId)).toEqual([ADA, BOB])
    expect(filterMentionCandidates(candidates, 'love').map(c => c.userId)).toEqual([ADA])
    expect(filterMentionCandidates(candidates, '')).toHaveLength(2)
  })
})
