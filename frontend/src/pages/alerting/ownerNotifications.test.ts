import { describe, expect, it } from 'vitest'

import {
  notifyOwnersResultSummary,
  ownerNotificationReason,
  ownerNotificationStatusLabel,
  ownerNotificationTone,
  ownerNotificationWho,
  ownersLabel,
} from './ownerNotifications'

describe('ownersLabel', () => {
  it('names each owner with an @', () => {
    expect(ownersLabel([
      { user_id: 'u-1', name: 'anna' },
      { user_id: 'u-2', name: 'oleg' },
    ])).toBe('Owners: @anna, @oleg')
  })

  it('is null when there is nobody to name', () => {
    expect(ownersLabel([])).toBeNull()
    expect(ownersLabel(undefined)).toBeNull()
    expect(ownersLabel(null)).toBeNull()
  })
})

describe('ownerNotificationWho', () => {
  it('falls back to the address when the user has no name', () => {
    expect(ownerNotificationWho({ user_id: null, name: null, email: 'a@x.io', status: 'sent' })).toBe('a@x.io')
    expect(ownerNotificationWho({ user_id: 'u', name: 'Anna', email: 'a@x.io', status: 'sent' })).toBe('Anna')
  })
})

describe('notifyOwnersResultSummary', () => {
  it('says so when nobody could be notified', () => {
    expect(notifyOwnersResultSummary([])).toMatch(/No owners to notify/)
  })

  it('separates who was emailed from who was not, and why', () => {
    expect(notifyOwnersResultSummary([
      { user_id: 'u-1', name: 'Anna', email: 'a@x.io', status: 'sent' },
      { user_id: 'u-2', name: 'Oleg', email: 'o@x.io', status: 'skipped', error: 'SMTP not configured' },
      { user_id: 'u-3', name: 'Ivan', email: 'i@x.io', status: 'failed' },
    ])).toBe('Emailed Anna. Not sent to Oleg (SMTP not configured), Ivan (failed).')
  })

  it('gives the cooldown reason for an owner notified moments ago', () => {
    expect(notifyOwnersResultSummary([
      { user_id: 'u-1', name: 'Anna', email: 'a@x.io', status: 'sent', sent_at: '2026-09-27T10:00:00Z' },
      { user_id: 'u-2', name: 'Oleg', email: 'o@x.io', status: 'skipped', error: 'notified 4 minutes ago' },
    ])).toBe('Emailed Anna. Not sent to Oleg (notified 4 minutes ago).')
  })

  it('does not call a still-pending row unsent', () => {
    expect(notifyOwnersResultSummary([
      { user_id: 'u-1', name: 'Anna', email: 'a@x.io', status: 'pending', sent_at: null },
    ])).toBe('Still sending to Anna.')
  })
})

describe('owner notification status', () => {
  it('labels a pending row as sending, in a neutral tone', () => {
    expect(ownerNotificationStatusLabel('pending')).toBe('sending')
    expect(ownerNotificationTone('pending')).toBe('neutral')
    expect(ownerNotificationTone('sent')).toBe('success')
    expect(ownerNotificationTone('failed')).toBe('danger')
    expect(ownerNotificationTone('skipped')).toBe('neutral')
  })

  it('prefers the server reason over the status word', () => {
    expect(ownerNotificationReason({ user_id: 'u', name: 'A', email: 'a@x.io', status: 'skipped', error: ' notified 2 minutes ago ' }))
      .toBe('notified 2 minutes ago')
    expect(ownerNotificationReason({ user_id: 'u', name: 'A', email: 'a@x.io', status: 'failed', error: null }))
      .toBe('failed')
  })
})
