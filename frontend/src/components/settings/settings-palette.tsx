import { useCallback, useEffect, useRef, useState } from 'react'
import { Command } from 'cmdk'
import { ChevronLeft, Folder, LayoutDashboard, LogOut, Search, SlidersHorizontal } from 'lucide-react'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Kbd } from '@/components/primitives/kbd'
import { PALETTE_ITEM_CLASS } from '@/components/palette-item'
import { SETTINGS_CONTENT_ID } from './landmarks'
import { visibleGroupsAll } from './nav'
import { projectHomePath } from '@/lib/navigation'
import type { Project } from '@/types'

type PaletteIcon = React.ComponentType<{ className?: string; style?: React.CSSProperties }>

interface PaletteRow {
  /**
   * cmdk's identity for the row. Namespaced per kind: cmdk marks a row selected
   * by comparing this string against the list's current value, so a section and
   * a project sharing a name would otherwise both announce as selected.
   */
  value: string
  label: string
  hint: string
  /** Extra words that find the row ("timezone" -> General), never shown. */
  keywords?: readonly string[]
  icon: PaletteIcon
  active?: boolean
  onSelect: () => void
}

interface PaletteGroup {
  heading: string
  rows: PaletteRow[]
}

/**
 * Substring, not a score: nothing here is ranked, a row shows or it does not.
 * Every word of the query has to appear somewhere in the row (label, hint or
 * keywords), so "api key" and "dark mode" find their section instead of
 * failing on the space (#238 JR-19).
 */
function matchesQuery(query: string, row: PaletteRow): boolean {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (tokens.length === 0) return true
  const haystack = [row.label, row.hint, ...(row.keywords ?? [])].join(' ').toLowerCase()
  return tokens.every(token => haystack.includes(token))
}

/**
 * Ctrl+K inside the Settings takeover.
 *
 * The app-wide palette cannot serve this area: it reads `useParams().slug` to
 * decide what it is scoped to, no /settings/* route declares one, and its
 * fallback is `projects[0]` — so on a takeover bound to acme-ios it searched
 * acme-android's knowledge and navigated into it, while offering none of that
 * project's own destinations (its project groups are empty without an active
 * project). The same reasoning that keeps `useSettingsSlug` from falling back to
 * the first project (tripl-jfm3.32) applies to searching from here.
 *
 * So this one offers only what a route with no project in scope can honestly
 * reach: every settings section the rail lists, the projects by name, the way
 * back out, and Sign out. Every row leaves through `onLeave` / `onSignOut`,
 * which is where the unsaved-changes guard sits (tripl-l8v2) — a palette that
 * navigated on its own would walk straight past it.
 */
export function SettingsCommandPalette({
  activePath,
  backHref,
  backLabel = 'Back to project',
  isOwner,
  projects,
  onLeave,
  onSwitchProject,
  onSignOut,
}: {
  /** Current section path (e.g. 'project/general'), marked as the current row. */
  activePath: string
  /** Where "Back to project" returns to; '/workspace' when nothing is bound. */
  backHref: string
  /** The rail's words for the same link ("Back to Acme iOS"), so both name
   *  where it really goes (ST-4). */
  backLabel?: string
  isOwner: boolean
  projects: readonly Pick<Project, 'name' | 'slug'>[]
  /** Guarded navigation. `settingsPath` is null for a destination outside /settings. */
  /** Navigate away. The destination is all a caller needs: the settings
   *  layout's blocker asks about the URL itself, so nothing here has to
   *  classify it (tripl-l33u.14). */
  onLeave: (href: string) => void
  /** Rebind the Project sections to this project without leaving settings
   *  (ST-6). Omitted, the palette offers no such rows. */
  onSwitchProject?: (slug: string) => void
  onSignOut: () => void
}) {
  const [open, setOpenState] = useState(false)
  const [query, setQuery] = useState('')

  // Whoever asked for the palette, so Esc can hand focus straight back. Ctrl+K
  // is a window-level shortcut, so on a freshly loaded page nothing is focused
  // and Radix's own restore target is <body> (tripl-jfm3.68).
  const openerRef = useRef<HTMLElement | null>(null)

  // Opening touches the ref, closing must not: every palette row closes the
  // dialog, so a combined setter would make each row's onSelect a ref reader and
  // the react-hooks lint rejects passing such closures through any function.
  const openPalette = useCallback(() => {
    const active = document.activeElement
    openerRef.current = active instanceof HTMLElement && active !== document.body ? active : null
    setOpenState(true)
  }, [])

  const closePalette = useCallback(() => {
    setQuery('')
    setOpenState(false)
  }, [])

  const setOpen = useCallback(
    (next: boolean) => {
      if (next) openPalette()
      else closePalette()
    },
    [openPalette, closePalette],
  )

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const isToggle =
        (event.key === 'k' || event.key === 'K') && (event.metaKey || event.ctrlKey)
      if (!isToggle) return
      const target = event.target
      // Same rule as the app palette: while typing in a field, Ctrl+K belongs
      // to the field. Settings pages are almost entirely fields.
      if (
        !open &&
        target instanceof HTMLElement &&
        target.closest('input, textarea, [contenteditable="true"]')
      ) {
        return
      }
      event.preventDefault()
      setOpen(!open)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setOpen])

  /** Opener → the takeover's content landmark. Never <body>. */
  const restoreFocus = useCallback(() => {
    const candidates = [openerRef.current, document.getElementById(SETTINGS_CONTENT_ID)]
    for (const candidate of candidates) {
      if (candidate?.isConnected) {
        candidate.focus()
        return
      }
    }
  }, [])

  const run = (action: () => void) => {
    closePalette()
    action()
  }

  const leaveRows: PaletteRow[] = [
    ...(backHref === '/workspace'
      ? []
      : [
          {
            value: `nav:${backHref}`,
            label: backLabel,
            hint: backHref,
            icon: ChevronLeft,
            onSelect: () => run(() => onLeave(backHref)),
          },
        ]),
    {
      value: 'nav:/workspace',
      label: 'All projects',
      hint: '/workspace',
      icon: LayoutDashboard,
      onSelect: () => run(() => onLeave('/workspace')),
    },
  ]

  // The rail's own groups, in the rail's order and under its labels, so the
  // palette reads as the same map of the area. Owner-only sections are filtered
  // exactly as the rail filters them.
  const sectionGroups: PaletteGroup[] = visibleGroupsAll(isOwner).map(group => ({
    heading: `${group.label} settings`,
    rows: group.items.map(item => ({
      value: `section:${item.path}`,
      label: item.label,
      hint: `/settings/${item.path}`,
      keywords: item.keywords,
      icon: item.icon,
      active: item.path === activePath,
      onSelect: () => run(() => onLeave(`/settings/${item.path}`)),
    })),
  }))

  // Named projects, not a guess: the slug is the hint, so a project stays
  // findable by the string an engineer would type.
  const projectRows: PaletteRow[] = projects.map(project => ({
    value: `project:${project.slug}`,
    label: project.name,
    hint: project.slug,
    icon: Folder,
    // The project's front door, as "Back to project" uses (#250 JR-1).
    onSelect: () => run(() => onLeave(projectHomePath(project.slug))),
  }))

  // The same projects as places to point the Project settings at, staying in
  // the area: the rows above leave it for the project's app (ST-6).
  const projectSettingsRows: PaletteRow[] = onSwitchProject
    ? projects.map(project => ({
        value: `project-settings:${project.slug}`,
        label: `Project settings for ${project.name}`,
        hint: project.slug,
        icon: SlidersHorizontal,
        onSelect: () => run(() => onSwitchProject(project.slug)),
      }))
    : []

  const accountRows: PaletteRow[] = [
    {
      value: 'account:sign-out',
      label: 'Sign out',
      hint: 'End this session',
      icon: LogOut,
      onSelect: () => run(onSignOut),
    },
  ]

  // Filtered inline rather than through a helper: every row carries an
  // `onSelect` closure, and handing that array to a function makes the
  // react-hooks lint assume the callee may read a captured ref during render.
  const groups: PaletteGroup[] = [
    { heading: 'Navigate', rows: leaveRows },
    ...sectionGroups,
    { heading: 'Projects', rows: projectRows },
    { heading: 'Switch project', rows: projectSettingsRows },
    { heading: 'Account', rows: accountRows },
  ]
    .map(group => ({
      heading: group.heading,
      rows: group.rows.filter(row => matchesQuery(query, row)),
    }))
    // cmdk short-circuits a group's `hidden` computation under
    // `shouldFilter={false}`, so an emptied group would keep its heading
    // standing over nothing. The group has to go, not just its rows.
    .filter(group => group.rows.length > 0)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        showCloseButton={false}
        className="overflow-hidden p-0 sm:max-w-[560px] gap-0"
        // Take over Radix's focus restore: it aims at whatever was focused when
        // the dialog mounted, which for a global Ctrl+K is usually <body>. The
        // app palette's chain ends at `#main-content`, which this area does not
        // render — the takeover names its own landmark.
        onCloseAutoFocus={event => {
          event.preventDefault()
          restoreFocus()
        }}
      >
        <DialogTitle className="sr-only">Settings command palette</DialogTitle>
        <Command
          label="Settings command palette"
          // Off, always — the rows are filtered above. cmdk's filter is also a
          // sort, and the rail's reading order is part of the map.
          shouldFilter={false}
          className="flex max-h-[440px] w-full min-w-0 flex-col"
        >
          <div
            className="flex items-center gap-2 border-b px-3.5 py-3 border-border-subtle"
          >
            <Search className="h-3.5 w-3.5 text-fg-tertiary" />
            <Command.Input
              // eslint-disable-next-line jsx-a11y/no-autofocus -- command palette search: focus on explicit ⌘K invocation is expected UX
              autoFocus
              value={query}
              onValueChange={setQuery}
              placeholder="Search settings and projects…"
              className="flex-1 bg-transparent text-body outline-none placeholder:text-[var(--fg-subtle)]"
            />
            <Kbd>esc</Kbd>
          </div>
          <Command.List className="flex-1 overflow-y-auto py-1.5">
            {groups.length === 0 && (
              <div
                className="px-3.5 py-8 text-center text-body-sm text-fg-tertiary"
              >
                No matches.
              </div>
            )}
            {groups.map(group => (
              <Command.Group
                key={group.heading}
                heading={group.heading}
                className="px-1.5 py-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-1.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:micro-label [&_[cmdk-group-heading]]:text-[var(--fg-faint)]"
              >
                {group.rows.map(row => {
                  const Icon = row.icon
                  return (
                    <Command.Item
                      key={row.value}
                      value={row.value}
                      onSelect={row.onSelect}
                      className={PALETTE_ITEM_CLASS}
                    >
                      <Icon className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" />
                      <span className="min-w-0 flex-1 truncate">{row.label}</span>
                      {row.active && (
                        <span
                          className="shrink-0 micro-label text-fg-tertiary"
                        >
                          current
                        </span>
                      )}
                      <span
                        className="mono shrink-0 truncate text-micro text-fg-tertiary"
                      >
                        {row.hint}
                      </span>
                    </Command.Item>
                  )
                })}
              </Command.Group>
            ))}
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
