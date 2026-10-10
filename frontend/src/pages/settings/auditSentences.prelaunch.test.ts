import { describe, expect, it } from 'vitest'

import { actionSentence, actionTone, hasWrittenSentence } from './auditSentences'

describe('audit sentences for the pre-launch wording', () => {
  // "Planned event" means an event of the tracking plan everywhere else; the
  // anomaly windows are "expected windows" on Annotations and the Volume tab.
  it('calls an anomaly window an expected window, not a planned event', () => {
    expect(actionSentence('planned_event.create')).toBe('Created expected window')
    expect(actionSentence('planned_event.delete')).not.toMatch(/planned event/i)
  })

  // An owner or admin minting a reset link for a member hands out a way into
  // that account, so the row reads as a sentence and is flagged.
  it('writes a sentence for a password reset link and marks it as sensitive', () => {
    expect(hasWrittenSentence('user.password_reset_link')).toBe(true)
    expect(actionSentence('user.password_reset_link')).toBe('Created a password reset link')
    expect(actionTone('user.password_reset_link')).toBe('danger')
  })
})
