import { describe, expect, it } from 'vitest'

import type { AuditEntry } from '@/types'
import {
  actionOptionLabels,
  actionSentence,
  actionTone,
  dayLabel,
  displayTarget,
  groupByDay,
  targetPath,
  toIsoOrUndef,
} from './auditSentences'

function entry(overrides: Partial<AuditEntry>): AuditEntry {
  return {
    id: 'e1',
    user_id: null,
    user_email: 'pm@example.com',
    project_id: 'p1',
    branch_id: null,
    branch_name: '',
    action: 'event.update',
    target_type: 'event',
    target_id: 't1',
    target_name: 'purchase',
    project_slug: 'demo',
    created_at: '2026-09-24T10:00:00Z',
    ...overrides,
  }
}

describe('auditSentences', () => {
  it('reads and tones the platform console actions (F20)', () => {
    expect(actionSentence('org.suspend')).toBe('Suspended the organization')
    expect(actionSentence('org.unsuspend')).toBe('Reinstated the organization')
    expect(actionSentence('platform.step_in')).toBe('Started a read-only step-in')
    expect(actionSentence('platform.step_in_end')).toBe('Ended a read-only step-in')
    expect(actionSentence('platform.admin_grant')).toBe('Granted platform admin to')
    expect(actionSentence('platform.admin_revoke')).toBe('Revoked platform admin from')
    expect(actionTone('org.suspend')).toBe('danger')
    expect(actionTone('org.unsuspend')).toBe('success')
    expect(actionTone('platform.step_in')).toBe('warning')
    expect(actionTone('platform.admin_revoke')).toBe('danger')
    expect(actionSentence('platform.license_set')).toBe('Installed the Enterprise license')
    expect(actionSentence('platform.license_clear')).toBe('Removed the Enterprise license')
    expect(actionTone('platform.license_clear')).toBe('danger')
    expect(actionSentence('org.audit_retention.update')).toBe('Changed how long the audit log is kept')
    expect(actionTone('org.audit_retention.update')).toBe('warning')
    expect(actionSentence('org.escalation_policy.create')).toBe('Created the escalation policy')
    expect(actionSentence('org.alert_route.delete')).toBe('Deleted the alert route')
    expect(actionTone('org.alert_route.delete')).toBe('danger')
  })

  it('tones an action by the suffix of its verb', () => {
    expect(actionTone('event.bulk_delete')).toBe('danger')
    expect(actionTone('plan_branch.merge')).toBe('success')
    expect(actionTone('alert_rule.snooze')).toBe('warning')
    expect(actionTone('scan_job.snapshot')).toBe('neutral')
    expect(actionTone('project.member_add')).toBe('success')
    expect(actionTone('project.member_update')).toBe('warning')
    expect(actionTone('project.member_remove')).toBe('danger')
  })

  it('reads an action code as a past-tense sentence', () => {
    expect(actionSentence('plan_branch.approve')).toBe('Approved branch')
    expect(actionSentence('metric_definition.create')).toBe('Created metric')
    expect(actionSentence('widget.frobnicate')).toBe('Frobnicate widget')
    expect(actionSentence('project.member_add')).toBe('Added a member to project')
    expect(actionSentence('project.member_remove')).toBe('Removed a member from project')
  })

  it('reads every docs-catalog action as a sentence (F22)', () => {
    expect(actionSentence('doc.create')).toBe('Created note')
    expect(actionSentence('doc.update')).toBe('Updated note')
    expect(actionSentence('doc.move')).toBe('Moved note')
    expect(actionSentence('doc.delete')).toBe('Deleted note')
    expect(actionSentence('doc.folder_delete')).toBe('Deleted a folder of notes')
    expect(actionSentence('doc.restore')).toBe('Restored an earlier revision of note')
    expect(actionSentence('doc.import')).toBe('Imported notes')
    expect(actionTone('doc.folder_delete')).toBe('danger')
    expect(actionTone('doc.create')).toBe('success')
  })

  it('names the code only where two option labels would read alike', () => {
    const labels = actionOptionLabels(['event.delete', 'event.bulk_delete', 'event.create'])
    expect(labels.get('event.delete')).toBe('Deleted event (event.delete)')
    expect(labels.get('event.bulk_delete')).toBe('Deleted event (event.bulk_delete)')
    expect(labels.get('event.create')).toBe('Created event')
  })

  it('shortens a UUID target name and falls back to the target type', () => {
    expect(displayTarget({ target_name: '0b7c2f1e-1111-4222-8333-444455556666', target_type: 'scan_job' })).toBe('0b7c2f1e')
    expect(displayTarget({ target_name: null, target_type: 'scan_job' })).toBe('scan_job')
  })

  it('links a target that has a page, never a deleted one', () => {
    expect(targetPath(entry({ target_type: 'variable', action: 'variable.update' }))).toBe('/p/demo/variables/t1')
    expect(targetPath(entry({ action: 'event.delete' }))).toBeNull()
    expect(targetPath(entry({ target_type: 'alert_rule' }))).toBeNull()
  })

  it('labels today and yesterday by name', () => {
    const now = new Date(2026, 8, 24, 15, 0)
    expect(dayLabel(new Date(2026, 8, 24, 9, 0).toISOString(), now)).toBe('Today')
    expect(dayLabel(new Date(2026, 8, 23, 9, 0).toISOString(), now)).toBe('Yesterday')
    expect(dayLabel('not a date', now)).toBe('')
  })

  it('groups consecutive entries of one day', () => {
    const groups = groupByDay([
      entry({ id: 'a', created_at: '2020-01-02T12:00:00Z' }),
      entry({ id: 'b', created_at: '2020-01-02T11:00:00Z' }),
      entry({ id: 'c', created_at: '2020-01-01T12:00:00Z' }),
    ])
    expect(groups.map((g) => g.entries.map((e) => e.id))).toEqual([['a', 'b'], ['c']])
  })

  it('pins a date input to the start or end of the local day', () => {
    expect(toIsoOrUndef('')).toBeUndefined()
    expect(toIsoOrUndef('2026-09-24')).toBe(new Date('2026-09-24T00:00:00.000').toISOString())
    expect(toIsoOrUndef('2026-09-24', true)).toBe(new Date('2026-09-24T23:59:59.999').toISOString())
  })
})
