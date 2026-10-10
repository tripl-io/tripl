import { describe, expect, it } from 'vitest'
import { bulkFormStatus } from './bulkFormStatus'

const READY = {
  hasEventTypes: true,
  typeChosen: true,
  cannotPaste: false,
  lineCount: 2,
  checking: false,
  readyCount: 2,
}

describe('bulkFormStatus', () => {
  it('opens on a muted next step, not a red error, before anything is touched', () => {
    expect(bulkFormStatus({ ...READY, typeChosen: false, lineCount: 0, readyCount: 0 })).toEqual({
      text: 'Pick an event type, then paste one event per line',
      tone: 'muted',
    })
  })

  it('points a project with no event types at making one first', () => {
    expect(
      bulkFormStatus({ ...READY, hasEventTypes: false, typeChosen: false, lineCount: 0, readyCount: 0 }),
    ).toEqual({ text: 'Create an event type first', tone: 'muted' })
  })

  it('keeps the steps still to take muted', () => {
    expect(bulkFormStatus({ ...READY, lineCount: 0, readyCount: 0 })?.tone).toBe('muted')
    expect(bulkFormStatus({ ...READY, checking: true })).toEqual({ text: 'Checking the names…', tone: 'muted' })
  })

  it('turns red only for what is wrong with what was given', () => {
    expect(bulkFormStatus({ ...READY, cannotPaste: true })).toEqual({
      text: 'This event type cannot be filled from a pasted list',
      tone: 'danger',
    })
    expect(bulkFormStatus({ ...READY, readyCount: 0 })).toEqual({ text: 'No line can be created', tone: 'danger' })
  })

  it('says nothing once Create can go', () => {
    expect(bulkFormStatus(READY)).toBeNull()
  })
})
