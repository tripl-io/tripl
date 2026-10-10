/**
 * The organization the app is acting in (F20 PR7, GH #273).
 *
 * Every project lives in one organization, and a project slug is only a name
 * inside it, so the UI addresses a project as `/o/{org}/p/{slug}/…` and the API
 * as `/api/v1/orgs/{org}/projects/{slug}/…`. The organization comes from the
 * URL — the `/o/:org` segment, or a settings address's `?org=`; a route
 * without one (a legacy `/p/…` link, a bare `/settings/…`) acts in the last
 * organization used in this tab, else in any tab, else the user's first.
 *
 * This module is the plain-TypeScript half: one module-level value that the
 * API client, the query-key builders, the link builders and the storage
 * helpers read synchronously. `ActiveOrgProvider` (components/active-org-
 * provider.tsx) is the only writer; it sets the value while it renders, before
 * any child renders, so a child reads the organization its own URL names.
 *
 * `null` means "no organization known" — no session yet, a user in none, a
 * test that mounts a page without the provider. Everything that reads it then
 * behaves as it did before organizations existed: legacy `/p/` links, unscoped
 * API paths (the server acts in the default organization), unprefixed storage
 * keys. It is never guessed from here.
 */

/** The organization every pre-F20 instance was migrated into. */
export const DEFAULT_ORG_SLUG = 'default'

/** localStorage: the organization last opened, for routes that name none. */
export const LAST_ORG_STORAGE_KEY = 'tripl-last-org'

let activeOrgSlug: string | null = null

/** The organization the app acts in right now, or `null` when none is known. */
export function currentOrgSlug(): string | null {
  return activeOrgSlug
}

/** Set by `ActiveOrgProvider` only (and by tests). */
export function setCurrentOrgSlug(slug: string | null): void {
  activeOrgSlug = slug
}

/**
 * The organization last opened, THIS tab's first: sessionStorage belongs to one
 * tab, so a second tab opening another organization cannot move the first one's
 * org-less routes (`/settings/*`) under it. localStorage, shared by every tab,
 * only seeds a tab that has opened no organization yet.
 */
export function readLastOrgSlug(): string | null {
  try {
    const tabOrg = sessionStorage.getItem(LAST_ORG_STORAGE_KEY)
    if (tabOrg) return tabOrg
  } catch {
    /* no sessionStorage: fall through to the shared value */
  }
  try {
    return localStorage.getItem(LAST_ORG_STORAGE_KEY)
  } catch {
    return null
  }
}

export function writeLastOrgSlug(slug: string): void {
  try {
    sessionStorage.setItem(LAST_ORG_STORAGE_KEY, slug)
  } catch {
    /* storage unavailable: the shared value below still applies */
  }
  try {
    localStorage.setItem(LAST_ORG_STORAGE_KEY, slug)
  } catch {
    /* storage unavailable: the fallback is the first organization */
  }
}

// ---------------------------------------------------------------------------
// Query keys.
// ---------------------------------------------------------------------------

/**
 * The root segment of every key: the organization the app acts in (F20 PR7).
 *
 * Two organizations may each hold a project `web`, and the same `/projects`
 * list means a different thing in each, so a cache filled in one must never
 * answer in the other. Every builder below starts with this, so switching
 * organization reads (and invalidates) a disjoint set of caches, and nothing has
 * to be cleared on a switch. The session (`['auth', …]`) is the one exception:
 * it is the account's, in no organization.
 *
 * Empty when no organization is known (no provider mounted, a user in none):
 * the keys then read as they did before organizations. Code that inspects a
 * key's segments by position goes through {@link keySegment}.
 */
export const orgRoot = (): readonly string[] => {
  const org = currentOrgSlug()
  return org ? [org] : []
}

/**
 * Segment `index` of `queryKey` counted after the organization root, so
 * `keySegment(eventsKey, 0) === 'events'` whether or not an organization is
 * active. For predicates that read a key by position.
 */
export function keySegment(queryKey: readonly unknown[], index: number): unknown {
  return queryKey[orgRoot().length + index]
}

const ORG_PATH = /^\/o\/([^/?#]+)/

/** The `:org` of an `/o/:org/…` pathname, or `null` for any other address. */
export function orgFromPathname(pathname: string): string | null {
  const match = ORG_PATH.exec(pathname)
  return match?.[1] ? decodeURIComponent(match[1]) : null
}

/** The query parameter that names the organization of an org-less route. */
export const ORG_QUERY_PARAM = 'org'

/**
 * The organization a location names: the `/o/:org` of its path, else, on the
 * settings takeover (`/settings/*`, which has no org segment), its `?org=`.
 */
export function orgFromLocation(pathname: string, search: string): string | null {
  const fromPath = orgFromPathname(pathname)
  if (fromPath) return fromPath
  if (pathname !== '/settings' && !pathname.startsWith('/settings/')) return null
  return new URLSearchParams(search).get(ORG_QUERY_PARAM) || null
}

/**
 * A settings-takeover address (`/settings/…`, with or without a query string
 * or hash) bound to an organization by `?org=`, so the page acts where the link
 * was made and not wherever another tab went last. With no organization known
 * the address is unchanged.
 */
export function settingsPath(path: string, org: string | null | undefined = currentOrgSlug()): string {
  if (!org) return path
  const hashAt = path.indexOf('#')
  const hash = hashAt === -1 ? '' : path.slice(hashAt)
  const beforeHash = hashAt === -1 ? path : path.slice(0, hashAt)
  const queryAt = beforeHash.indexOf('?')
  const pathname = queryAt === -1 ? beforeHash : beforeHash.slice(0, queryAt)
  const params = new URLSearchParams(queryAt === -1 ? '' : beforeHash.slice(queryAt + 1))
  params.set(ORG_QUERY_PARAM, org)
  return `${pathname}?${params.toString()}${hash}`
}

/**
 * Which organization to act in.
 *
 * The URL wins, even when it names an organization the user does not belong to:
 * the server answers 404 there, which is the honest answer to that link.
 * Otherwise the last organization used, while the user still belongs to it,
 * else the first they belong to, else none.
 */
export function pickActiveOrg(
  urlOrg: string | null,
  orgs: readonly { slug: string }[],
  lastOrg: string | null,
): string | null {
  if (urlOrg) return urlOrg
  if (lastOrg && orgs.some((org) => org.slug === lastOrg)) return lastOrg
  return orgs[0]?.slug ?? null
}

/**
 * A pathname with its organization prefix taken off: `/o/acme/p/web/events`
 * reads as `/p/web/events`, and the organization's own page (`/o/acme`) as the
 * portfolio `/workspace`. For code that parses a location (breadcrumbs, the tab
 * title, "which nav item is active"), which then needs one shape, not two.
 */
export function stripOrgPrefix(pathname: string): string {
  const match = ORG_PATH.exec(pathname)
  if (!match) return pathname
  const rest = pathname.slice(match[0].length)
  if (rest === '' || rest === '/' || rest === '/workspace' || rest === '/workspace/') return '/workspace'
  return rest
}

/**
 * The address of a project page: `/o/{org}/p/{slug}{rest}`, or the legacy
 * `/p/{slug}{rest}` when no organization is known (it redirects once one is).
 * `rest` starts with `/`, `?` or `#`, or is empty for the project root.
 */
export function projectPath(org: string | null | undefined, slug: string | undefined, rest = ''): string {
  return org ? `/o/${encodeURIComponent(org)}/p/${slug}${rest}` : `/p/${slug}${rest}`
}

/** An organization's own page — its workspace (portfolio). */
export function orgHomePath(org: string | null | undefined, rest = ''): string {
  return org ? `/o/${encodeURIComponent(org)}${rest}` : `/workspace${rest}`
}

/** The active organization's workspace: where "All projects" goes. */
export function workspacePath(): string {
  return orgHomePath(currentOrgSlug())
}

/**
 * A legacy in-app address (`/p/{slug}/…`, e.g. a `target_path` the server
 * wrote before organizations) moved under the active organization. Anything
 * else is returned as it is.
 */
export function withActiveOrg(path: string): string {
  const org = currentOrgSlug()
  if (!org || !path.startsWith('/p/')) return path
  return `/o/${encodeURIComponent(org)}${path}`
}

// ---------------------------------------------------------------------------
// Per-organization browser storage.
// ---------------------------------------------------------------------------

const ORG_STORAGE_PREFIX = 'o:'

/**
 * The storage key of a per-project or per-user value, inside the active
 * organization: `o:{org}:{key}`. Two organizations may each hold a project
 * `web`, and its remembered branch, tour step or saved views must not leak from
 * one into the other. With no organization known the key is unchanged.
 */
export function orgStorageKey(key: string, org: string | null = currentOrgSlug()): string {
  return org ? `${ORG_STORAGE_PREFIX}${org}:${key}` : key
}

/** localStorage: set once the pre-organization keys have been moved. */
export const ORG_STORAGE_MIGRATED_KEY = 'tripl-org-storage-migrated'

/**
 * localStorage, under {@link orgStorageKey}: the last project the user opened.
 * The sidebar, Settings and a slug rename write it; Settings reads it for a
 * bare address.
 */
export const LAST_PROJECT_SLUG_KEY = 'tripl-last-project-slug'

/**
 * The key families {@link orgStorageKey} scopes: an exact key, or a prefix
 * ending in `:` / `.` followed by a project slug or id.
 */
export const ORG_SCOPED_STORAGE_KEYS: readonly string[] = [
  LAST_PROJECT_SLUG_KEY,
  'tripl.eventsSavedViews',
]
export const ORG_SCOPED_STORAGE_PREFIXES: readonly string[] = [
  'tripl-branch:',
  'tripl-onboarding-dismissed:',
  'tripl-tour:',
  'tripl-demo-scenario:',
  'tripl-demo-welcome-dismissed:',
  'tripl.eventsChartOpen.',
]

function isOrgScopedKey(key: string): boolean {
  return (
    ORG_SCOPED_STORAGE_KEYS.includes(key) ||
    ORG_SCOPED_STORAGE_PREFIXES.some((prefix) => key.startsWith(prefix))
  )
}

/**
 * One-time move of the pre-organization keys under the default organization,
 * where every project lived before F20: `tripl-branch:web` becomes
 * `o:default:tripl-branch:web`. A value already present under the new key
 * wins. Runs once per browser; later calls return at once.
 */
export function migrateLegacyOrgStorage(storage: Storage | undefined = safeLocalStorage()): void {
  if (!storage) return
  try {
    if (storage.getItem(ORG_STORAGE_MIGRATED_KEY) === '1') return
    const keys: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (key && isOrgScopedKey(key)) keys.push(key)
    }
    for (const key of keys) {
      const value = storage.getItem(key)
      const target = orgStorageKey(key, DEFAULT_ORG_SLUG)
      if (value !== null && storage.getItem(target) === null) storage.setItem(target, value)
      storage.removeItem(key)
    }
    storage.setItem(ORG_STORAGE_MIGRATED_KEY, '1')
  } catch {
    /* storage unavailable or full: nothing to move, or try again next load */
  }
}

function safeLocalStorage(): Storage | undefined {
  try {
    return typeof localStorage === 'undefined' ? undefined : localStorage
  } catch {
    return undefined
  }
}

/**
 * The organization a legacy `/p/{slug}` address belongs to (F20 PR7): the one
 * organization among the user's whose project list holds the slug; else the
 * default organization, when the user is in it; else the user's only (or
 * first) organization. `holders` are the user's organizations known to hold
 * the slug.
 */
export function resolveLegacyProjectOrg(
  orgs: readonly { slug: string }[],
  holders: readonly { slug: string }[],
): string | null {
  if (holders.length === 1 && holders[0]) return holders[0].slug
  if (orgs.some((org) => org.slug === DEFAULT_ORG_SLUG)) return DEFAULT_ORG_SLUG
  return orgs[0]?.slug ?? null
}
