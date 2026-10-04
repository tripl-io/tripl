import { describe, expect, it } from 'vitest'
import { cn } from './utils'

describe('cn with the app type and radius scale', () => {
  it('keeps a scale size next to a colour class', () => {
    expect(cn('text-body-sm', 'text-fg-subtle')).toBe('text-body-sm text-fg-subtle')
    expect(cn('text-micro', 'text-fg-tertiary')).toBe('text-micro text-fg-tertiary')
    expect(cn('text-micro', 'text-fg-subtle')).toBe('text-micro text-fg-subtle')
    expect(cn('text-heading', 'text-fg')).toBe('text-heading text-fg')
    expect(cn('text-display', 'text-fg')).toBe('text-display text-fg')
  })

  it('lets a later size override an earlier one', () => {
    expect(cn('text-body-sm', 'text-caption')).toBe('text-caption')
    expect(cn('text-body-sm', 'text-title')).toBe('text-title')
  })

  it('lets a micro-label replace the size, weight and tracking before it', () => {
    expect(cn('px-2 text-body-sm font-semibold tracking-tight', 'micro-label text-fg-tertiary')).toBe(
      'px-2 micro-label text-fg-tertiary',
    )
    expect(cn('micro-label', 'text-fg-tertiary')).toBe('micro-label text-fg-tertiary')
  })

  it('treats the named radii as radii', () => {
    expect(cn('rounded-md', 'rounded-control')).toBe('rounded-control')
  })
})
