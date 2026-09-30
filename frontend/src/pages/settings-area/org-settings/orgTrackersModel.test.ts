import { describe, expect, it } from 'vitest'
import { orgTrackerDefaultsFixture } from '@/test/orgTrackerDefaults'
import {
  buildTrackerUpdate,
  jiraGroupWarning,
  trackerDisplayValue,
  trackerDraftInvalid,
  trackerFieldError,
  trackerSource,
  trackerUpdateEmpty,
  withTrackerEdit,
} from './orgTrackersModel'

describe('orgTrackersModel (F20 PR12)', () => {
  it('reads values and sources, never a secret', () => {
    const defaults = orgTrackerDefaultsFixture()
    expect(trackerDisplayValue(defaults, {}, 'jira.base_url')).toBe('https://acme.atlassian.net')
    expect(trackerDisplayValue(defaults, {}, 'jira.api_token')).toBe('')
    expect(trackerSource(defaults, 'jira.base_url')).toBe('org')
    expect(trackerSource(defaults, 'jira.project_key')).toBe('default')
  })

  it("clears an organization's value when its field is emptied, and drops no-op edits", () => {
    const defaults = orgTrackerDefaultsFixture()
    expect(withTrackerEdit(defaults, {}, 'jira.base_url', '')).toEqual({ 'jira.base_url': null })
    // Nothing to clear: an empty field the organization never set is no edit.
    expect(withTrackerEdit(defaults, {}, 'jira.project_key', '  ')).toEqual({})
    expect(withTrackerEdit(defaults, { 'linear.team_id': 'OPS' }, 'linear.team_id', 'ENG')).toEqual({})
    // A blank secret keeps the stored one; null removes it.
    expect(withTrackerEdit(defaults, {}, 'linear.api_key', '')).toEqual({})
    expect(withTrackerEdit(defaults, {}, 'linear.api_key', null)).toEqual({ 'linear.api_key': null })
    expect(withTrackerEdit(orgTrackerDefaultsFixture({ sources: {} }), {}, 'linear.api_key', null)).toEqual({})
  })

  it('sends only what changed, per tracker', () => {
    expect(
      buildTrackerUpdate({
        'jira.project_key': ' ENG ',
        'jira.api_token': '  ',
        'linear.api_key': null,
      }),
    ).toEqual({ jira: { project_key: 'ENG' }, linear: { api_key: null } })
    expect(buildTrackerUpdate({ 'jira.api_token': 'tok' })).toEqual({ jira: { api_token: 'tok' } })
    expect(trackerUpdateEmpty(buildTrackerUpdate({ 'jira.api_token': '' }))).toBe(true)
  })

  it('checks formats as the backend does', () => {
    expect(trackerFieldError('jira.base_url', 'http://acme.atlassian.net')).toMatch(/https URL/)
    expect(trackerFieldError('jira.base_url', 'https://acme.atlassian.net')).toBeNull()
    expect(trackerFieldError('jira.project_key', 'e')).toMatch(/2–32/)
    expect(trackerFieldError('jira.project_key', 'eng')).toBeNull()
    expect(trackerFieldError('jira.auth_email', 'nope')).toMatch(/email/)
    expect(trackerFieldError('linear.team_id', 'a b')).toMatch(/64/)
    expect(trackerFieldError('jira.base_url', null)).toBeNull()
    expect(trackerDraftInvalid({ 'jira.auth_email': 'nope' })).toBe(true)
  })

  it('warns while the Jira site, account and token are not all set together (critique #13)', () => {
    const defaults = orgTrackerDefaultsFixture()
    expect(jiraGroupWarning(defaults, {})).toBeNull()
    expect(jiraGroupWarning(defaults, { 'jira.api_token': null })).toMatch(/go together/)
    const empty = orgTrackerDefaultsFixture({
      jira: { base_url: '', auth_email: '', api_token_configured: false, project_key: '' },
      sources: {},
    })
    expect(jiraGroupWarning(empty, {})).toBeNull()
    expect(jiraGroupWarning(empty, { 'jira.base_url': 'https://acme.atlassian.net' })).toMatch(/go together/)
  })
})
