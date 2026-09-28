import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { CountBadge } from '@/components/primitives/count-badge'
import type { NavGroup, NavItem } from '@/lib/navigation'
import type { EventType } from '@/types'
import {
  hasNavCount,
  isUrgentCount,
  navIconColor,
  navLinkClass,
  navLinkStyle,
} from './sidebar-style'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const EVENT_TYPES_EXPANDED_KEY = 'tripl-sidebar-event-types-expanded'
/** Event-type rows shown under Events before a "Show N more" row (#238 SH-9). */
const EVENT_TYPE_ROW_CAP = 6

/**
 * A nav count (DS-6, DS-28): the CountBadge geometry, fed the pre-formatted
 * figure ("1.2K") the nav model carries. Neutral grey for counts; solid red
 * only for unacknowledged alerts (Alerting's open incidents). The figure
 * stays in the link's accessible name ("Anomalies 9"). The neutral pill is
 * --surface with a hairline, not CountBadge's --surface-active: in light that
 * is --sidebar-hover, so the pill vanished into a hovered row.
 */
function NavCount({ count, urgent }: { count: string; urgent: boolean }) {
  return (
    <CountBadge
      count={count}
      urgent={urgent}
      aria-hidden={undefined}
      data-urgent={urgent || undefined}
      className={urgent ? undefined : 'bg-surface ring-1 ring-inset ring-border'}
    />
  )
}

function eventTypeChildActive(eventTypes: EventType[], navSlug: string, currentPath: string): boolean {
  return eventTypes.some((eventType) => {
    const href = eventTypeEventsHref(navSlug, eventType.name)
    return currentPath === href || currentPath.startsWith(`${href}/`)
  })
}

export function NavGroupSection({
  group,
  currentPath,
  eventTypes,
  navSlug,
}: {
  group: NavGroup
  currentPath: string
  eventTypes: EventType[]
  navSlug: string | undefined
}) {
  return (
    <div className="mb-3">
      <div
        className="px-2 pb-1 micro-label text-fg-tertiary"
      >
        {group.label}
      </div>
      <div className="flex flex-col gap-px">
        {group.items.map((item) => {
          if (item.id === 'events' && navSlug) {
            return (
              <EventsNavCategory
                key={item.id}
                item={item}
                eventTypes={eventTypes}
                navSlug={navSlug}
                currentPath={currentPath}
              />
            )
          }
          return <NavRow key={item.id} item={item} active={item.match(currentPath)} />
        })}
      </div>
    </div>
  )
}

function NavRow({ item, active }: { item: NavItem; active: boolean }) {
  const Icon = item.icon
  return (
    <Link
      to={item.href}
      aria-current={active ? 'page' : undefined}
      className={navLinkClass(active, 'text-body-sm')}
      style={navLinkStyle(active)}
    >
      <Icon
        className="size-3.5 shrink-0"
        style={{ color: navIconColor(active) }}
        aria-hidden="true"
      />
      <span className="flex-1 truncate text-left">{item.label}</span>
      {hasNavCount(item) && <NavCount count={item.count} urgent={isUrgentCount(item)} />}
    </Link>
  )
}

function useEventTypesExpanded() {
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem(EVENT_TYPES_EXPANDED_KEY) === '1'
    } catch {
      return false
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(EVENT_TYPES_EXPANDED_KEY, expanded ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [expanded])
  return [expanded, setExpanded] as const
}

/**
 * Events with its per-type filters nested under it (#238 SH-9 / JR-24).
 *
 * The type rows open the Events list filtered by type, so they belong to
 * Events. They used to hang under "Event types", whose own row opens the
 * schema configuration: one visual tree, two kinds of page, and on a filtered
 * list the marker sat under "Event types" while the breadcrumb said Events.
 * Events stays lit as the section while a type is open; only the type row is
 * the current page. Capped at six rows with "Show N more", remembered.
 */
function EventsNavCategory({
  item,
  eventTypes,
  navSlug,
  currentPath,
}: {
  item: NavItem
  eventTypes: EventType[]
  navSlug: string
  currentPath: string
}) {
  const [expanded, setExpanded] = useEventTypesExpanded()
  const childActive = eventTypeChildActive(eventTypes, navSlug, currentPath)
  const sectionActive = item.match(currentPath)
  // The row is the page only when no type filter is: then it is "all events".
  const rowActive = sectionActive && !childActive
  const Icon = item.icon
  const overflow = eventTypes.length - EVENT_TYPE_ROW_CAP
  const activeIndex = eventTypes.findIndex((eventType) => {
    const href = eventTypeEventsHref(navSlug, eventType.name)
    return currentPath === href || currentPath.startsWith(`${href}/`)
  })
  // An open type beyond the cap is shown anyway: the current page is never
  // folded away.
  const showAll = expanded || overflow <= 0 || activeIndex >= EVENT_TYPE_ROW_CAP
  const visible = showAll ? eventTypes : eventTypes.slice(0, EVENT_TYPE_ROW_CAP)

  return (
    <div>
      <Link
        to={item.href}
        aria-current={rowActive ? 'page' : undefined}
        className={navLinkClass(rowActive, 'text-body-sm')}
        style={rowActive ? navLinkStyle(true) : { color: childActive ? 'var(--fg)' : 'var(--fg-muted)' }}
      >
        <Icon
          className="size-3.5 shrink-0"
          style={{ color: navIconColor(sectionActive) }}
          aria-hidden="true"
        />
        <span className="flex-1 truncate text-left">{item.label}</span>
        {hasNavCount(item) && <NavCount count={item.count} urgent={false} />}
      </Link>
      {eventTypes.length > 0 && (
        <div
          className="mt-px ml-[15px] flex flex-col gap-px border-l pl-2 border-border-subtle"
        >
          {visible.map((eventType) => (
            <EventTypeNavRow
              key={eventType.id}
              eventType={eventType}
              href={eventTypeEventsHref(navSlug, eventType.name)}
              currentPath={currentPath}
            />
          ))}
          {overflow > 0 && activeIndex < EVENT_TYPE_ROW_CAP && (
            <button
              type="button"
              onClick={() => setExpanded(!expanded)}
              aria-expanded={expanded}
              className="flex min-h-7 items-center rounded-control px-2 py-1 text-left text-caption transition-colors hover:bg-sidebar-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg-tertiary"
            >
              {expanded ? 'Show fewer' : `Show ${overflow} more`}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function EventTypeNavRow({
  eventType,
  href,
  currentPath,
}: {
  eventType: EventType
  href: string
  currentPath: string
}) {
  const active = currentPath === href || currentPath.startsWith(`${href}/`)

  return (
    <Link
      to={href}
      aria-current={active ? 'page' : undefined}
      className={navLinkClass(active, 'text-body-sm')}
      style={navLinkStyle(active)}
    >
      <span
        aria-hidden="true"
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: eventType.color || 'var(--fg-faint)' }}
      />
      <span className="min-w-0 flex-1 truncate text-left">{eventType.display_name}</span>
    </Link>
  )
}

function eventTypeEventsHref(slug: string, eventTypeName: string): string {
  return projectPath(currentOrgSlug(), slug, `/events/${eventTypeName}`)
}

export function EmptyNav({ loading }: { loading: boolean }) {
  return (
    <div className="px-2 py-2 text-caption text-fg-tertiary">
      {loading ? 'Loading projects…' : 'No projects yet'}
    </div>
  )
}
