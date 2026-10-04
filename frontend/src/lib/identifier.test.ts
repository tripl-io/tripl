import { describe, expect, it } from 'vitest'
import { toIdentifier } from './identifier'

describe('toIdentifier', () => {
  it('derives snake_case, transliterating Cyrillic and stripping accents', () => {
    expect(toIdentifier('Checkout Conversion!', 'fb')).toBe('checkout_conversion')
    expect(toIdentifier('Конверсия оплаты', 'fb')).toBe('konversiya_oplaty')
    expect(toIdentifier('Café crème', 'fb')).toBe('cafe_creme')
  })

  it('falls back for a name with nothing to derive from, and stays empty for none', () => {
    expect(toIdentifier('转化率', 'metric_x')).toBe('metric_x')
    expect(toIdentifier('   ', 'metric_x')).toBe('')
  })
})
