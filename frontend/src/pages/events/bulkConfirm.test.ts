import { describe, expect, it } from 'vitest'
import { BULK_CONFIRM_THRESHOLD, bulkUpdateConfirmation, selectionQuestion } from './bulkConfirm'

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

// Bulk delete and the bulk changes ask through one sentence: bulk delete once
// built its own and asked "Delete 1 selected events?".
describe('selectionQuestion', () => {
  it('agrees the noun with a single event', () => {
    expect(selectionQuestion('Delete', 1, 1)).toBe('Delete 1 selected event?')
    expect(selectionQuestion('Delete', 3, 3)).toBe('Delete 3 selected events?')
  })

  it('says how much of the selection is off screen, in the app locale', () => {
    expect(selectionQuestion('Set status to Live for', 12_000, 40)).toBe(
      'Set status to Live for 12,000 selected events? Only 40 of them are on screen — 11,960 are outside the current filter or page.',
    )
  })

  it('is what the bulk change confirmation asks', () => {
    const confirmation = bulkUpdateConfirmation({
      selectedCount: 1,
      selectedVisibleCount: 1,
      actionLabel: 'Set status to Archived',
      archives: true,
    })
    expect(confirmation?.message).toBe('Set status to Archived for 1 selected event?')
  })
})
