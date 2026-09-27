import { describe, expect, it } from 'vitest'
import type { MetaFieldDefinition } from '@/types'
import { branchTicket, ticketKeyFromBranchName, ticketMetaField } from './branchTicket'

const field = (over: Partial<MetaFieldDefinition>): MetaFieldDefinition => ({
  id: 'mf-1',
  project_id: 'p',
  name: 'jira',
  display_name: 'Jira',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  default_value: null,
  link_template: 'https://tracker.example/browse/${value}',
  order: 0,
  sensitivity: 'none',
  ...over,
})

describe('ticketKeyFromBranchName', () => {
  it('reads a Jira-style key off the front of the name', () => {
    expect(ticketKeyFromBranchName('PROJ-4770')).toBe('PROJ-4770')
    expect(ticketKeyFromBranchName('PROJ-4500-2')).toBe('PROJ-4500')
    expect(ticketKeyFromBranchName('  AB1-7 checkout ')).toBe('AB1-7')
  })

  it('has nothing to say for a name that is not a key', () => {
    expect(ticketKeyFromBranchName('checkout-v2')).toBeNull()
    expect(ticketKeyFromBranchName('proj-4770')).toBeNull()
    expect(ticketKeyFromBranchName('4770')).toBeNull()
  })
})

describe('ticketMetaField', () => {
  it('is the first string field whose template can take a key', () => {
    const url = field({ id: 'mf-url', field_type: 'url', link_template: null })
    const bare = field({ id: 'mf-bare', link_template: null })
    const jira = field({ id: 'mf-jira' })
    expect(ticketMetaField([url, bare, jira])?.id).toBe('mf-jira')
    expect(ticketMetaField([url, bare])).toBeNull()
  })
})

describe('branchTicket', () => {
  it('joins the branch name and the linking field into one href', () => {
    expect(branchTicket('PROJ-4770', [field({})])).toEqual({
      key: 'PROJ-4770',
      href: 'https://tracker.example/browse/PROJ-4770',
      field: field({}),
    })
  })

  it('needs both halves', () => {
    expect(branchTicket('PROJ-4770', [field({ link_template: null })])).toBeNull()
    expect(branchTicket('release-notes', [field({})])).toBeNull()
    expect(branchTicket(null, [field({})])).toBeNull()
  })
})
