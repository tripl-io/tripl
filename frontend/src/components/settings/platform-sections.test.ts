import { describe, expect, it } from 'vitest'
import { itemVisible, sectionLabel, WORKSPACE_GROUPS } from './nav'
import { PLATFORM_SECTION_KEYS, PLATFORM_SECTION_LABELS, platformSectionPath } from './platform-sections'

describe('the Platform sections', () => {
  const platform = WORKSPACE_GROUPS.find((group) => group.label === 'Platform')
  // The rail's own Platform items; the Enterprise console teasers lead the group.
  const own = (platform?.items ?? []).filter((item) => item.tag === undefined)

  it('are the rail\'s Platform items, in its order, under the names the pages use', () => {
    expect(own.map((item) => item.path)).toEqual(PLATFORM_SECTION_KEYS.map(platformSectionPath))
    expect(own.map((item) => item.label)).toEqual(PLATFORM_SECTION_KEYS.map((key) => PLATFORM_SECTION_LABELS[key]))
  })

  it('name a page what its rail item is called', () => {
    // The page the rail calls Mail relay used to offer "Reset Email to defaults".
    for (const key of PLATFORM_SECTION_KEYS) {
      expect(sectionLabel(platformSectionPath(key))).toBe(PLATFORM_SECTION_LABELS[key])
    }
  })

  it('are a platform admin\'s alone, whatever the organization role', () => {
    for (const item of own) {
      expect(itemVisible(item, true, false), item.label).toBe(false)
      expect(itemVisible(item, false, true), item.label).toBe(true)
    }
  })
})

describe('the Organization search settings', () => {
  it('are named apart from the Enterprise search across projects', () => {
    // Two "Search" items in one group read as one page.
    expect(sectionLabel('organization/search')).toBe('Semantic search')
    expect(sectionLabel('organization/project-search')).toBe('Search projects')
  })
})
