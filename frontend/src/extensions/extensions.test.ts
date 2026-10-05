import { describe, expect, it } from 'vitest'
import { Lock } from 'lucide-react'
import { WORKSPACE_GROUPS, withExtensionItems, type SettingsNavGroup } from '@/components/settings/nav'
import {
  EXTENSIONS,
  enterpriseTeaser,
  enterpriseTeasers,
  extensionRoutes,
  extensionSettingsSection,
} from '.'
import { ENTERPRISE_TEASERS, visibleTeasers, type EnterpriseTeaser } from './teasers'

const item = (id: string) => ({ id, label: id, icon: Lock, path: `organization/${id}` })

const GROUPS: SettingsNavGroup[] = [
  { label: 'Organization', sub: '', desc: '', items: [item('a'), item('b')] },
  { label: 'Account', sub: '', desc: '', items: [item('c')] },
]

describe('frontend extensions', () => {
  it('carries no extension in a Community build', () => {
    expect(EXTENSIONS).toEqual([])
    expect(extensionRoutes).toEqual([])
  })

  it('places extension items after the item they name, or at the end of their group', () => {
    const placed = withExtensionItems(GROUPS, [
      { group: 'Organization', after: 'a', item: item('x') },
      { group: 'Organization', after: 'missing', item: item('y') },
      { group: 'Organization', item: item('z') },
      { group: 'Nowhere', item: item('lost') },
    ])
    expect(placed[0]?.items.map((entry) => entry.id)).toEqual(['a', 'x', 'b', 'y', 'z'])
    expect(placed[1]?.items.map((entry) => entry.id)).toEqual(['c'])
    // The groups passed in are left as they were.
    expect(GROUPS[0]?.items.map((entry) => entry.id)).toEqual(['a', 'b'])
  })

  it('keeps the rail order: Enterprise features where their pages would be', () => {
    const organization = WORKSPACE_GROUPS.find((group) => group.label === 'Organization')
    const items = organization?.items ?? []
    const ids = items.map((entry) => entry.id)
    const trackers = ids.indexOf('org-trackers')
    expect(ids.slice(trackers, trackers + 3)).toEqual(['org-trackers', 'org-sso', 'org-scim'])
    const limits = ids.indexOf('org-limits')
    expect(ids.slice(limits, limits + 3)).toEqual(['org-limits', 'inst-audit', 'org-audit-webhook'])
    // Every Enterprise feature is a teaser in this build.
    expect(items.filter((entry) => entry.tag === 'Enterprise').map((entry) => entry.id)).toEqual([
      'org-sso',
      'org-scim',
      'org-escalation',
      'inst-audit',
      'org-audit-webhook',
      'org-audit-retention',
    ])
    // The platform console leads the Platform group, before Runtime.
    const platform = WORKSPACE_GROUPS.find((group) => group.label === 'Platform')
    expect(platform?.items.slice(0, 3).map((entry) => entry.id)).toEqual([
      'platform-orgs',
      'platform-users',
      'runtime',
    ])
  })

  it('finds no extension settings section in a Community build', () => {
    expect(extensionSettingsSection('organization/audit-webhook')).toBeUndefined()
    expect(extensionSettingsSection('organization/sso')).toBeUndefined()
  })
})

describe('Enterprise teasers', () => {
  const section = (id: string) => ({ item: item(id) })
  const teaser = (id: string): EnterpriseTeaser => ({
    group: 'Organization',
    item: { ...item(id), tag: 'Enterprise', ownerOnly: true },
    summary: `${id} does things`,
  })

  it('shows a teaser only where no extension provides the section', () => {
    const shown = visibleTeasers([teaser('sso'), teaser('scim')], [section('sso')])
    expect(shown.map((entry) => entry.item.id)).toEqual(['scim'])
  })

  it('shows the teaser of every feature no extension provides', () => {
    expect(enterpriseTeasers.map((entry) => entry.item.id)).toEqual([
      'org-sso',
      'org-scim',
      'org-escalation',
      'inst-audit',
      'org-audit-webhook',
      'org-audit-retention',
      'platform-orgs',
      'platform-users',
    ])
    expect(enterpriseTeaser('organization/sso')?.item.label).toBe('Single sign-on')
    expect(enterpriseTeaser('instance/audit')?.item.label).toBe('Audit log')
  })

  it('tags every teaser Enterprise, for owners and admins or platform admins only, once each', () => {
    const ids = ENTERPRISE_TEASERS.map((entry) => entry.item.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const entry of ENTERPRISE_TEASERS) {
      expect(entry.item.tag).toBe('Enterprise')
      // An organization's feature for its owners and admins; the console for platform admins.
      expect(entry.group === 'Platform' ? entry.item.platformOnly : entry.item.ownerOnly).toBe(true)
      expect(entry.summary.length).toBeGreaterThan(20)
    }
  })

  it('places an item before the one it names, ahead of after', () => {
    const placed = withExtensionItems(GROUPS, [{ ...teaser('x'), before: 'a', after: 'a' }])
    expect(placed[0]?.items[0]?.id).toBe('x')
  })

  it('places a teaser in the rail like an extension item', () => {
    const placed = withExtensionItems(GROUPS, [{ ...teaser('x'), after: 'a' }])
    expect(placed[0]?.items.map((entry) => [entry.id, entry.tag])).toEqual([
      ['a', undefined],
      ['x', 'Enterprise'],
      ['b', undefined],
    ])
  })
})
