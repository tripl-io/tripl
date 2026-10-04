import { orgStorageKey } from '@/lib/activeOrg'

export type EventsSavedView = {
  name: string
  tab: string
  params: string
  updated_at: string
}

/** The URL keys that describe the list itself, and so belong in a view. */
const VIEW_PARAM_KEYS = new Set(['q', 'status', 'tag', 'silent_days', 'reviewed', 'questions', 'sort'])

function isViewParam(key: string): boolean {
  return VIEW_PARAM_KEYS.has(key) || key.startsWith('f.') || key.startsWith('m.')
}

/**
 * The part of a query string a saved view keeps: the filter, search and sort
 * keys, sorted so two orders of the same filters compare equal. Everything else
 * stays out, `?branch=` above all: a view saved on a branch reopened that
 * branch after it had merged.
 */
export function viewParamsOf(params: URLSearchParams | string): string {
  const source = typeof params === 'string' ? new URLSearchParams(params) : params
  const pairs: [string, string][] = []
  source.forEach((value, key) => {
    if (isViewParam(key)) pairs.push([key, value])
  })
  pairs.sort(([ak, av], [bk, bv]) => (ak === bk ? av.localeCompare(bv) : ak.localeCompare(bk)))
  return new URLSearchParams(pairs).toString()
}

/**
 * `current` with its view keys replaced by `view`'s: whatever else the URL
 * carries (the branch, for one) is left as it is.
 */
export function applyViewParams(current: URLSearchParams, view: string): URLSearchParams {
  const next = new URLSearchParams()
  current.forEach((value, key) => {
    if (!isViewParam(key)) next.append(key, value)
  })
  new URLSearchParams(viewParamsOf(view)).forEach((value, key) => next.append(key, value))
  return next
}

type StoredEventsSavedViews = Record<string, Record<string, Omit<EventsSavedView, 'name'>>>

const STORAGE_KEY = 'tripl.eventsSavedViews'
let memoryStore: string | null = null

function readStore(): StoredEventsSavedViews {
  let raw: string | null
  try {
    raw = typeof localStorage.getItem === 'function' ? localStorage.getItem(orgStorageKey(STORAGE_KEY)) : null
  } catch {
    raw = memoryStore
  }
  raw ??= memoryStore
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as StoredEventsSavedViews
    }
  } catch {
    return {}
  }
  return {}
}

function writeStore(store: StoredEventsSavedViews) {
  const serialized = JSON.stringify(store)
  try {
    if (typeof localStorage.setItem === 'function') {
      localStorage.setItem(orgStorageKey(STORAGE_KEY), serialized)
      return
    }
  } catch {
    // Ignore storage quota/private-mode failures; saved views are optional UI state.
  }
  memoryStore = serialized
}

export function loadEventsSavedViews(slug: string): EventsSavedView[] {
  const projectViews = readStore()[slug] ?? {}
  return Object.entries(projectViews)
    .map(([name, view]) => ({ name, ...view }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

export function saveEventsSavedView(
  slug: string,
  view: Pick<EventsSavedView, 'name' | 'tab' | 'params'>,
): EventsSavedView[] {
  const name = view.name.trim()
  if (!name) return loadEventsSavedViews(slug)
  const store = readStore()
  store[slug] = {
    ...(store[slug] ?? {}),
    [name]: {
      tab: view.tab,
      params: view.params,
      updated_at: new Date().toISOString(),
    },
  }
  writeStore(store)
  return loadEventsSavedViews(slug)
}

export function deleteEventsSavedView(slug: string, name: string): EventsSavedView[] {
  const store = readStore()
  const projectViews = store[slug]
  if (!projectViews) return []
  delete projectViews[name]
  if (Object.keys(projectViews).length === 0) {
    delete store[slug]
  }
  writeStore(store)
  return loadEventsSavedViews(slug)
}
