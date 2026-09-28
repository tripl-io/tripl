import type { CSSProperties } from 'react'
import type { NavItem } from '@/lib/navigation'
import { cn } from '@/lib/utils'
import { settingsPath } from '@/lib/activeOrg'

// Shared by the expanded sidebar, its nav sections and the collapsed rail.

/**
 * Nav icons name a section, never a status: they stay neutral and only the
 * current page's icon takes the accent. The danger/warning tint used to stack
 * with the red count and the bell for the same anomalies (DS-28).
 */
export function navIconColor(active: boolean): string {
  return active ? 'var(--accent)' : 'var(--fg-subtle)'
}

/**
 * One look for every sidebar link: hover and keyboard focus come from CSS (the
 * old inline `style.background` writes had no keyboard twin and could stick
 * after the active item changed), and the current page carries a bar on its
 * left edge, so "you are here" is not told by a tint alone (SHELL-24). Hover
 * and active use the sidebar's own tokens: `surface-hover` on the sunken
 * sidebar was a 1.02:1 change in light theme, i.e. no feedback (DS-11).
 */
// 28px rows (`min-h-7 py-1`), not 30: with a real project's event types the
// last Observe/Govern rows sat under the fold at 1440×900 (#238 SH-10).
export const NAV_LINK_CLASS =
  'relative flex min-h-7 items-center gap-2 rounded-control px-2 py-1 font-medium no-underline transition-colors hover:bg-sidebar-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]'
export const ACTIVE_MARKER_CLASS =
  "before:absolute before:inset-y-1.5 before:left-0 before:w-[2px] before:rounded-full before:bg-[var(--accent)] before:content-['']"
export const ACTIVE_ROW_CLASS = 'bg-sidebar-active hover:bg-sidebar-active'

export function navLinkClass(active: boolean, extra?: string): string {
  return cn(NAV_LINK_CLASS, active && ACTIVE_MARKER_CLASS, active && ACTIVE_ROW_CLASS, extra)
}

export function navLinkStyle(active: boolean): CSSProperties {
  return { color: active ? 'var(--fg)' : 'var(--fg-muted)' }
}

/**
 * A zero is not news: in an empty project every "0" drew the eye to nothing
 * (#238 SH-38). Only a count worth reading gets a pill.
 */
export function hasNavCount(item: NavItem): item is NavItem & { count: string } {
  return item.count !== undefined && item.count !== '0'
}

/** Only an open-incident backlog is an unacknowledged alert; see NavCount. */
export function isUrgentCount(item: NavItem): boolean {
  return item.urgent === true
}

export const ICON_BUTTON_CLASS =
  'relative flex h-8 w-8 items-center justify-center rounded-md no-underline transition-colors hover:bg-sidebar-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]'

/** Project settings, bound to THIS project by the address (SHELL-20). */
export function projectSettingsHref(slug: string): string {
  return settingsPath(`/settings/project/general?project=${encodeURIComponent(slug)}`)
}

/** "owner" -> "Owner". */
export function capitalize(value: string): string {
  return value ? value[0]!.toUpperCase() + value.slice(1) : value
}
