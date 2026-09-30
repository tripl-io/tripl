import type { PlatformOrg, PlatformOrgStatus } from '@/api/platform'
import { settingsPath } from '@/lib/navigation'

/** The server's bounds on a suspension or step-in reason. */
export const REASON_MAX = 500
/** The server's bounds on a step-in's lifetime, and its default. */
export const STEP_IN_TTL_MIN = 5
export const STEP_IN_TTL_MAX = 240
export const STEP_IN_TTL_DEFAULT = 60
/** Rows per console page. */
export const PLATFORM_PAGE_SIZE = 50

/** What is wrong with a reason, or `null` when the server will take it. */
export function reasonError(reason: string): string | null {
  const trimmed = reason.trim()
  if (!trimmed) return 'Give a reason. It is recorded in the audit log.'
  if (reason.length > REASON_MAX) return `Keep the reason under ${REASON_MAX} characters.`
  if (reason.includes('\u0000')) return 'The reason contains a character that cannot be stored.'
  return null
}

/** What is wrong with a step-in length in minutes, or `null`. */
export function ttlError(value: string): string | null {
  const minutes = Number(value)
  if (!value.trim() || !Number.isInteger(minutes)) return 'Enter a whole number of minutes.'
  if (minutes < STEP_IN_TTL_MIN || minutes > STEP_IN_TTL_MAX) {
    return `Between ${STEP_IN_TTL_MIN} and ${STEP_IN_TTL_MAX} minutes.`
  }
  return null
}

export const STATUS_LABEL: Readonly<Record<PlatformOrgStatus, string>> = {
  active: 'Active',
  suspended: 'Suspended',
  deleting: 'Deleting',
}

export const STATUS_TONE: Readonly<Record<PlatformOrgStatus, 'success' | 'warning' | 'danger'>> = {
  active: 'success',
  suspended: 'warning',
  deleting: 'danger',
}

/** Only an active organization can be suspended; only a suspended one reinstated. */
export function canSuspend(org: Pick<PlatformOrg, 'status'>): boolean {
  return org.status === 'active'
}

export function canUnsuspend(org: Pick<PlatformOrg, 'status'>): boolean {
  return org.status === 'suspended'
}

/**
 * An active or a suspended organization can be stepped into: suspension blocks
 * members and keys, but a read-only step-in is how an operator investigates it.
 * A deleting one is on its way out.
 */
export function canStepIn(org: Pick<PlatformOrg, 'status'>): boolean {
  return org.status === 'active' || org.status === 'suspended'
}

/** "1 member", "3 projects". */
export function countLabel(count: number, singular: string, plural = `${singular}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? singular : plural}`
}

/** "Showing 51–100 of 230", or "No results". */
export function rangeLabel(offset: number, shown: number, total: number): string {
  if (total === 0 || shown === 0) return 'No results'
  return `Showing ${(offset + 1).toLocaleString()}–${(offset + shown).toLocaleString()} of ${total.toLocaleString()}`
}

/** The detail page of one organization in the console. */
export function platformOrgPath(slug: string): string {
  return settingsPath(`/settings/platform/orgs/${encodeURIComponent(slug)}`)
}
