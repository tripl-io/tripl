import { afterEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_ORG_SLUG,
  ORG_STORAGE_MIGRATED_KEY,
  keySegment,
  migrateLegacyOrgStorage,
  orgFromPathname,
  orgHomePath,
  orgRoot,
  orgStorageKey,
  LAST_ORG_STORAGE_KEY,
  orgFromLocation,
  pickActiveOrg,
  projectPath,
  readLastOrgSlug,
  settingsPath,
  writeLastOrgSlug,
  resolveLegacyProjectOrg,
  setCurrentOrgSlug,
  stripOrgPrefix,
  withActiveOrg,
} from './activeOrg'
import { projectsKey, projectEventsKey } from './queryKeys'

const ORGS = [{ slug: 'default' }, { slug: 'acme' }]

afterEach(() => {
  setCurrentOrgSlug(null)
  sessionStorage.removeItem(LAST_ORG_STORAGE_KEY)
  localStorage.removeItem(LAST_ORG_STORAGE_KEY)
})

describe('the active organization', () => {
  it('reads the organization from an /o/:org address only', () => {
    expect(orgFromPathname('/o/acme/p/web/events')).toBe('acme')
    expect(orgFromPathname('/o/acme')).toBe('acme')
    expect(orgFromPathname('/p/web/events')).toBeNull()
    expect(orgFromPathname('/settings/members')).toBeNull()
  })

  it('prefers the URL, then the last organization still held, then the first', () => {
    expect(pickActiveOrg('acme', ORGS, 'default')).toBe('acme')
    // A foreign organization in the URL still wins: the server answers 404 there.
    expect(pickActiveOrg('other', ORGS, 'default')).toBe('other')
    expect(pickActiveOrg(null, ORGS, 'acme')).toBe('acme')
    // A remembered organization the user has left is not acted in.
    expect(pickActiveOrg(null, ORGS, 'gone')).toBe('default')
    expect(pickActiveOrg(null, [], 'acme')).toBeNull()
  })
})

describe('addresses', () => {
  it('builds project and organization paths, legacy while no organization is known', () => {
    expect(projectPath('acme', 'web', '/events')).toBe('/o/acme/p/web/events')
    expect(projectPath('acme', 'web')).toBe('/o/acme/p/web')
    expect(projectPath(null, 'web', '/events')).toBe('/p/web/events')
    expect(orgHomePath('acme')).toBe('/o/acme')
    expect(orgHomePath(null)).toBe('/workspace')
  })

  it('strips the organization prefix for code that parses a location', () => {
    expect(stripOrgPrefix('/o/acme/p/web/events/all')).toBe('/p/web/events/all')
    expect(stripOrgPrefix('/o/acme')).toBe('/workspace')
    expect(stripOrgPrefix('/o/acme/')).toBe('/workspace')
    expect(stripOrgPrefix('/o/acme/workspace')).toBe('/workspace')
    expect(stripOrgPrefix('/p/web')).toBe('/p/web')
    expect(stripOrgPrefix('/settings/members')).toBe('/settings/members')
  })

  it('moves a server-written /p/ address under the active organization', () => {
    expect(withActiveOrg('/p/web/alerting')).toBe('/p/web/alerting')
    setCurrentOrgSlug('acme')
    expect(withActiveOrg('/p/web/alerting?item=x')).toBe('/o/acme/p/web/alerting?item=x')
    expect(withActiveOrg('/settings/profile')).toBe('/settings/profile')
    expect(withActiveOrg('/o/other/p/web')).toBe('/o/other/p/web')
  })
})

describe('the legacy /p/:slug redirect target', () => {
  it('is the one organization that holds the slug', () => {
    expect(resolveLegacyProjectOrg(ORGS, [{ slug: 'acme' }])).toBe('acme')
  })

  it('falls back to the default organization when none or several hold it', () => {
    expect(resolveLegacyProjectOrg(ORGS, [])).toBe(DEFAULT_ORG_SLUG)
    expect(resolveLegacyProjectOrg(ORGS, ORGS)).toBe(DEFAULT_ORG_SLUG)
  })

  it("is the user's only organization when they are not in the default one", () => {
    expect(resolveLegacyProjectOrg([{ slug: 'acme' }], [])).toBe('acme')
    expect(resolveLegacyProjectOrg([], [])).toBeNull()
  })
})

describe('query keys', () => {
  it('are rooted at the active organization, and unrooted with none', () => {
    expect(projectsKey()).toEqual(['projects'])
    expect(orgRoot()).toEqual([])
    setCurrentOrgSlug('acme')
    expect(projectsKey()).toEqual(['acme', 'projects'])
    expect(projectEventsKey('web')).toEqual(['acme', 'events', 'web'])
    setCurrentOrgSlug('default')
    expect(projectsKey()).not.toEqual(['acme', 'projects'])
  })

  it('reads a segment after the organization root', () => {
    setCurrentOrgSlug('acme')
    expect(keySegment(projectEventsKey('web'), 0)).toBe('events')
    expect(keySegment(projectEventsKey('web'), 1)).toBe('web')
    setCurrentOrgSlug(null)
    expect(keySegment(projectEventsKey('web'), 0)).toBe('events')
  })
})

describe('per-organization storage', () => {
  it('prefixes a key with the active organization', () => {
    expect(orgStorageKey('tripl-branch:web')).toBe('tripl-branch:web')
    setCurrentOrgSlug('acme')
    expect(orgStorageKey('tripl-branch:web')).toBe('o:acme:tripl-branch:web')
    expect(orgStorageKey('tripl-branch:web', 'default')).toBe('o:default:tripl-branch:web')
  })

  it('moves the pre-organization keys under the default organization, once', () => {
    localStorage.setItem('tripl-branch:web', 'br-1')
    localStorage.setItem('tripl-last-project-slug', 'web')
    localStorage.setItem('tripl.eventsChartOpen.web', '[]')
    localStorage.setItem('tripl-ui-theme', 'dark')
    localStorage.setItem('o:default:tripl-tour:web', '3')
    localStorage.setItem('tripl-tour:web', '1')

    migrateLegacyOrgStorage(localStorage)

    expect(localStorage.getItem('o:default:tripl-branch:web')).toBe('br-1')
    expect(localStorage.getItem('o:default:tripl-last-project-slug')).toBe('web')
    expect(localStorage.getItem('o:default:tripl.eventsChartOpen.web')).toBe('[]')
    expect(localStorage.getItem('tripl-branch:web')).toBeNull()
    // Not per-project state: left where it is.
    expect(localStorage.getItem('tripl-ui-theme')).toBe('dark')
    // A value already under the new key wins over the old one.
    expect(localStorage.getItem('o:default:tripl-tour:web')).toBe('3')
    expect(localStorage.getItem(ORG_STORAGE_MIGRATED_KEY)).toBe('1')

    localStorage.setItem('tripl-branch:api', 'br-2')
    migrateLegacyOrgStorage(localStorage)
    expect(localStorage.getItem('tripl-branch:api')).toBe('br-2')
  })
})

describe('the settings takeover names its organization', () => {
  it('reads ?org= on a settings address only, after the path', () => {
    expect(orgFromLocation('/settings/members', '?org=acme')).toBe('acme')
    expect(orgFromLocation('/settings', '?org=acme')).toBe('acme')
    expect(orgFromLocation('/o/beta/p/web', '?org=acme')).toBe('beta')
    expect(orgFromLocation('/p/web/events', '?org=acme')).toBeNull()
    expect(orgFromLocation('/settings/members', '')).toBeNull()
  })

  it('binds a settings address to the organization, keeping its query and hash', () => {
    setCurrentOrgSlug('acme')
    expect(settingsPath('/settings/members')).toBe('/settings/members?org=acme')
    expect(settingsPath('/settings/project/general?project=web')).toBe('/settings/project/general?project=web&org=acme')
    expect(settingsPath('/settings/invitations?invite=1#top', 'beta')).toBe('/settings/invitations?invite=1&org=beta#top')
    // An address that already names one is re-bound, not given two.
    expect(settingsPath('/settings/members?org=beta')).toBe('/settings/members?org=acme')
    setCurrentOrgSlug(null)
    expect(settingsPath('/settings/members')).toBe('/settings/members')
  })

  it("prefers this tab's last organization over the one another tab wrote", () => {
    writeLastOrgSlug('default')
    // Another tab opens acme: it shares localStorage, not this sessionStorage.
    localStorage.setItem(LAST_ORG_STORAGE_KEY, 'acme')
    expect(readLastOrgSlug()).toBe('default')
    // A new tab has no organization of its own yet and takes the shared one.
    sessionStorage.removeItem(LAST_ORG_STORAGE_KEY)
    expect(readLastOrgSlug()).toBe('acme')
  })
})
