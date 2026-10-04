import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FieldDefinition } from '@/types'
import {
  carryFieldValues,
  isNumberFieldValue,
  normalizeNumberFieldValue,
  normalizeTag,
  sunsetInputValue,
  sunsetIsoValue,
  withPendingChip,
} from './eventFormValues'

const field = (id: string, name: string, extra: Partial<FieldDefinition> = {}) =>
  ({
    id,
    name,
    display_name: name.toUpperCase(),
    field_type: 'string',
    is_required: false,
    enum_options: null,
    order: 0,
    ...extra,
  }) as unknown as FieldDefinition

describe('sunset date', () => {
  // Pinned off UTC: CI runs in UTC, where local wall time and UTC coincide, so
  // an implementation that read the picker as UTC would pass there.
  beforeEach(() => {
    vi.stubEnv('TZ', 'Asia/Tokyo')
  })
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('runs in a zone that is not UTC', () => {
    expect(new Date(2026, 9, 1).getTimezoneOffset()).toBe(-540)
  })

  it('round-trips an instant through the local input unchanged', () => {
    const iso = '2026-10-01T09:00:00.000Z'
    expect(sunsetIsoValue(sunsetInputValue(iso))).toBe(iso)
  })

  it('reads the input as local wall time', () => {
    // 09:00 in Tokyo (UTC+9) is midnight UTC.
    expect(sunsetIsoValue('2026-10-01T09:00')).toBe('2026-10-01T00:00:00.000Z')
    expect(sunsetInputValue('2026-10-01T00:05:00.000Z')).toBe('2026-10-01T09:05')
  })

  it('treats empty and unreadable values as no date', () => {
    expect(sunsetInputValue(null)).toBe('')
    expect(sunsetInputValue('not a date')).toBe('')
    expect(sunsetIsoValue('')).toBeNull()
    expect(sunsetIsoValue('garbage')).toBeNull()
  })
})

describe('number field values', () => {
  it('takes numbers, tokens and nothing', () => {
    expect(isNumberFieldValue('')).toBe(true)
    expect(isNumberFieldValue('12.5')).toBe(true)
    expect(isNumberFieldValue('-3')).toBe(true)
    expect(isNumberFieldValue('${price}')).toBe(true)
  })

  it('refuses other text', () => {
    expect(isNumberFieldValue('twelve')).toBe(false)
    expect(isNumberFieldValue('${')).toBe(false)
    expect(isNumberFieldValue('Infinity')).toBe(false)
    expect(isNumberFieldValue('1,000,5')).toBe(false)
    expect(isNumberFieldValue('1.000,5')).toBe(false)
  })

  it('takes a decimal comma, and sends it as a point', () => {
    // The decimal key of a comma-locale phone keyboard types `,`.
    expect(isNumberFieldValue('1,5')).toBe(true)
    expect(isNumberFieldValue('-0,25')).toBe(true)
    expect(normalizeNumberFieldValue('1,5')).toBe('1.5')
    expect(normalizeNumberFieldValue(' -0,25 ')).toBe('-0.25')
    expect(normalizeNumberFieldValue('12.5')).toBe('12.5')
    expect(normalizeNumberFieldValue('${price}')).toBe('${price}')
  })
})

describe('pending chip text', () => {
  it('adds what is typed, once, and never removes', () => {
    expect(withPendingChip(['a'], ' b ')).toEqual(['a', 'b'])
    expect(withPendingChip(['a'], 'a')).toEqual(['a'])
    expect(withPendingChip(['a'], '   ')).toEqual(['a'])
    expect(withPendingChip(['checkout'], 'Checkout', normalizeTag)).toEqual(['checkout'])
  })
})

describe('carrying field values across a type change', () => {
  it('moves values by field name', () => {
    const from = [field('a1', 'variant'), field('a2', 'payload')]
    const to = [field('b1', 'variant')]
    expect(carryFieldValues(from, to, { a1: 'b2', a2: '' })).toEqual({
      values: { b1: 'b2' },
      dropped: [],
    })
  })

  it('names what cannot follow: no field, or a control that cannot hold it', () => {
    const from = [field('a1', 'variant'), field('a2', 'payload'), field('a3', 'flag')]
    const to = [
      field('b1', 'variant', { field_type: 'enum', enum_options: ['a', 'b'] } as Partial<FieldDefinition>),
      field('b3', 'flag', { field_type: 'boolean' } as Partial<FieldDefinition>),
    ]
    expect(carryFieldValues(from, to, { a1: 'c', a2: '{}', a3: 'true' })).toEqual({
      values: { b3: 'true' },
      dropped: ['VARIANT', 'PAYLOAD'],
    })
  })
})
