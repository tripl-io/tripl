import { describe, expect, it } from 'vitest'
import { copyName } from './copyName'

describe('copyName', () => {
  it('takes `<name>_copy` while it is free', () => {
    expect(copyName('orders', new Set(['orders']))).toBe('orders_copy')
  })

  it('counts up past every copy already taken', () => {
    expect(copyName('orders', new Set(['orders', 'orders_copy', 'orders_copy_2']))).toBe('orders_copy_3')
  })
})
