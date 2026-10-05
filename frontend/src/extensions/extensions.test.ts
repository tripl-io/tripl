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
  it('carries the bundled enterprise extension and nothing else by default', () => {
    expect(EXTENSIONS.map((extension) => extension.name)).toEqual(['bundled-enterprise'])
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
    expect(ids[ids.indexOf('inst-audit') + 1]).toBe('org-audit-webhook')
    // Single sign-on and provisioning are Enterprise teasers in this build.
    expect(items.filter((entry) => entry.tag === 'Enterprise').map((entry) => entry.id)).toEqual([
      'org-sso',
      'org-scim',
    ])
  })

  it('finds an extension settings section by its path', () => {
    expect(extensionSettingsSection('organization/audit-webhook')?.access).toBe('orgOwner')
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

  it('shows the teasers of features no extension provides, and hides the bundled one', () => {
    expect(enterpriseTeasers.map((entry) => entry.item.id)).toEqual(['org-sso', 'org-scim'])
    expect(enterpriseTeaser('organization/sso')?.item.label).toBe('Single sign-on')
    // The audit webhook is still bundled: its real page, not a teaser.
    expect(enterpriseTeaser('organization/audit-webhook')).toBeUndefined()
  })

  it('tags every teaser Enterprise, for owners and admins only, once each', () => {
    const ids = ENTERPRISE_TEASERS.map((entry) => entry.item.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const entry of ENTERPRISE_TEASERS) {
      expect(entry.item.tag).toBe('Enterprise')
      expect(entry.item.ownerOnly).toBe(true)
      expect(entry.summary.length).toBeGreaterThan(20)
    }
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
