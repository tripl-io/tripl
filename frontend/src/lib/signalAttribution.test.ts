import { describe, expect, it } from 'vitest'
import type { SignalAttributionColumn } from '@/types'
import {
  attributionValueLabel,
  columnRemainder,
  columnScale,
  formatShare,
  formatSignedCount,
} from './signalAttribution'

const platform: SignalAttributionColumn = {
  column: 'platform',
  explained_share: 0.92,
  values: [
    { value: 'ios', delta: -3120, expected: 5000, actual: 1880, share: 0.92 },
    { value: 'android', delta: -300, expected: 4000, actual: 3700, share: 0.0885 },
  ],
}

describe('formatSignedCount', () => {
  it('signs with a real minus and groups thousands', () => {
    expect(formatSignedCount(-3120)).toBe('−3,120')
    expect(formatSignedCount(120)).toBe('+120')
    expect(formatSignedCount(0)).toBe('0')
    expect(formatSignedCount(-0.2)).toBe('−0.2')
  })
})

describe('formatShare', () => {
  it('clips to 0..100% and flags a tiny share', () => {
    expect(formatShare(0.92)).toBe('92%')
    expect(formatShare(1.4)).toBe('100%')
    expect(formatShare(-0.2)).toBe('0%')
    expect(formatShare(0.001)).toBe('<1%')
  })
})

describe('columnRemainder / columnScale', () => {
  it('makes the listed values and the remainder sum to the delta', () => {
    const remainder = columnRemainder(platform, -3390)
    expect(remainder).toBe(30)
    const listed = platform.values.reduce((sum, value) => sum + value.delta, 0)
    expect(listed + remainder).toBe(-3390)
    expect(columnScale(platform, -3390)).toBe(3120)
  })

  it('drops float dust', () => {
    expect(columnRemainder(platform, -3420.2)).toBe(0)
  })
})

describe('attributionValueLabel', () => {
  it('names an empty value', () => {
    expect(attributionValueLabel('')).toBe('(empty)')
    expect(attributionValueLabel('ios')).toBe('ios')
  })
})
