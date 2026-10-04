import { describe, expect, it } from 'vitest'
import { BULK_CONFIRM_THRESHOLD, bulkUpdateConfirmation } from './bulkConfirm'

describe('bulkUpdateConfirmation', () => {
  it('applies a small change to rows on screen without asking', () => {
    expect(
      bulkUpdateConfirmation({ selectedCount: 3, selectedVisibleCount: 3, actionLabel: 'Mark reviewed' }),
    ).toBeNull()
  })

  it('names the rows off screen when the change reaches them', () => {
    const confirmation = bulkUpdateConfirmation({
      selectedCount: 20,
      selectedVisibleCount: 3,
      actionLabel: 'Set status to Live',
    })

    expect(confirmation?.message).toMatch(/Only 3 of them are on screen — 17 are outside/)
  })

  it('asks before a large sweep and before any archive', () => {
    expect(
      bulkUpdateConfirmation({
        selectedCount: BULK_CONFIRM_THRESHOLD + 1,
        selectedVisibleCount: BULK_CONFIRM_THRESHOLD + 1,
        actionLabel: 'Mark reviewed',
      }),
    ).not.toBeNull()
    expect(
      bulkUpdateConfirmation({
        selectedCount: 1,
        selectedVisibleCount: 1,
        actionLabel: 'Set status to Archived',
        archives: true,
      })?.variant,
    ).toBe('danger')
  })

  it('always asks before a deprecation, so the dialog can list dependents (#257)', () => {
    const confirmation = bulkUpdateConfirmation({
      selectedCount: 1,
      selectedVisibleCount: 1,
      actionLabel: 'Set status to Deprecated',
      deprecates: true,
    })
    expect(confirmation).not.toBeNull()
    expect(confirmation?.variant).toBe('primary')
  })
})
