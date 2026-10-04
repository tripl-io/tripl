import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { formatDateTime } from '@/lib/datetime'
import type { AlertInboxGroup, AlertInboxListResponse, Role } from '@/types'

import { AlertingInbox, type InboxActionVariables } from './AlertingInbox'
import {
  EMPTY_INBOX_FILTERS,
  earliestReachableDay,
  type InboxFilterState,
} from './inboxFilters'
import { createNoteDraftStore } from './noteDraftStore'
import { recordInboxActionFailure } from './inboxActionErrors'
import { SEARCH_DEBOUNCE_MS } from '@/hooks/useDebouncedValue'
import { at } from '@/test/at'
import { type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

/**
 * A session at one role.
 *
 * The inbox reads the role from this context and nowhere else, so a test that
 * omits the provider is a test of "no role information" — which deliberately
 * keeps the write controls (see lib/permissions.ts).
 */
function authValue(role: Role): AuthContextValue {
  return {
    user: {
      id: 'user-1',
      email: 'someone@example.com',
      name: 'Someone',
      role,
      is_platform_admin: false,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

function makeGroup(overrides: Partial<AlertInboxGroup> = {}): AlertInboxGroup {
  return {
    correlation_group_id: 'grp-1',
    status: 'open',
    muted: false,
    muted_until: null,
    note: null,
    false_positive_count: 0,
    item_count: 1,
    delivery_count: 1,
    latest_bucket: '2026-08-11T10:00:00Z',
    first_delivery_at: '2026-08-11T09:00:00Z',
    latest_delivery_at: '2026-08-11T10:05:00Z',
    direction: 'drop',
    actual_count: 412,
    expected_count: 1010,
    percent_delta: -59.2,
    max_abs_percent_delta: null,
    scope_type: 'event',
    scope_types: ['event'],
    scope_ref: 'scope-a',
    event_id: null,
    scope_names: ['onboarding/reviews_carousel'],
    destination_names: ['TG'],
    rules: [{ id: 'rule-1', name: 'Volume rule' }],
    rule_names: ['Volume rule'],
    scan_names: ['Snowplow Pageviews (iOS)'],
    acted_at: null,
    acted_by: null,
    acted_by_name: null,
    ...overrides,
  }
}

/**
 * One list response, defaulted to the ordinary case: the whole documented
 * window fitted, so `window_truncated_at` is null.
 *
 * A helper rather than object literals for the same reason `makeGroup` is one —
 * the response grows fields, and every fixture that spells the shape out by
 * hand has to be revisited when it does. `window_truncated_at` is required and
 * always sent, so a literal cannot omit it; nor can it omit
 * `next_cursor`.
 */
function makeInbox(overrides: Partial<AlertInboxListResponse> = {}): AlertInboxListResponse {
  return { items: [makeGroup()], total: 1, window_truncated_at: null, next_cursor: null, ...overrides }
}

function renderInbox(
  overrides: Partial<Parameters<typeof AlertingInbox>[0]> = {},
  role: Persona = 'member',
) {
  const onAction = vi.fn<(variables: InboxActionVariables) => void>()
  // Selection is page-held state threaded in as props, exactly like the note
  // drafts and the expanded set above it, so the default here is
  // "nothing picked" and a test that cares supplies its own set.
  const toggleIncidentSelected = vi.fn<(id: string, selected: boolean) => void>()
  const onFiltersChange = vi.fn<(next: InboxFilterState) => void>()
  const setIncidentsSelected =
    vi.fn<(ids: readonly string[], selected: boolean) => void>()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <AuthContext.Provider value={authValue(role === 'viewer' ? 'member' : role)}>
      <PersonaProject persona={role}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <AlertingInbox
            slug="demo"
            inbox={makeInbox({ items: [makeGroup()], total: 1 })}
            isLoading={false}
            isError={false}
            loadError={null}
            pinnedGroup={null}
            hasRules
            statusFilter=""
            onStatusFilterChange={vi.fn()}
            filters={EMPTY_INBOX_FILTERS}
            onFiltersChange={onFiltersChange}
            onLoadMore={vi.fn()}
            hasMore={false}
            isLoadingMore={false}
            noteDraftStore={createNoteDraftStore()}
            expandedIncidents={new Set()}
            toggleIncident={vi.fn()}
            selectedIncidents={new Set()}
            toggleIncidentSelected={toggleIncidentSelected}
            setIncidentsSelected={setIncidentsSelected}
            onAction={onAction}
            pendingGroupIds={new Set()}
            actionErrors={new Map()}
            onGoToMonitors={vi.fn()}
            {...overrides}
          />
        </MemoryRouter>
      </QueryClientProvider>
      </PersonaProject>
    </AuthContext.Provider>,
  )
  return { ...utils, onAction, toggleIncidentSelected, setIncidentsSelected, onFiltersChange }
}

/**
 * The scope summary `makeGroup` produces, and therefore the target every button
 * on the card names.
 *
 * It contains a `/`, which is why the mute queries below are exact NAMES rather
 * than regexes: interpolating this into a `RegExp` would need escaping, and the
 * prefix forms are ambiguous anyway — "Mute <target>" is a strict prefix of
 * "Mute <target> for 1h", so `/^Mute onboarding/` matched the disclosure toggle
 * AND all four duration buttons and only ever passed because the panel happens
 * to be closed at click time. An exact string is matched in full by RTL, so it
 * picks out one button in either state.
 */
const TARGET = 'onboarding/reviews_carousel'

describe('AlertingInbox item and scope counts', () => {
  it('explains when repeated items share the visible scope names', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup({
          item_count: 8,
          scope_names: ['one', 'two', 'three', 'four'],
        })],
      }),
    })

    expect(screen.getByText('8 items')).toBeInTheDocument()
    expect(screen.getByText('(4 distinct scope names shown)')).toBeInTheDocument()
    // The headline is the first scope, with the rest counted rather than
    // listed (AL-12); the full list stays on the "+N more" title.
    expect(screen.getByText('one')).toBeInTheDocument()
    expect(screen.getByText('+3 more')).toHaveAttribute('title', 'one, two, three, four')
  })
})

/**
 * Every mute sentence in this file is written out as a literal, and stays that
 * way after the mute-copy rework.
 *
 * The card no longer composes those sentences itself — it calls `muteName`,
 * `muteChoiceName` and `unmuteName` from `@/lib/mutePresets`, the same
 * functions `MonitorsSection` and `MonitorDetailPage` now call. Deriving these
 * expectations from those functions would make both sides move together and
 * assert nothing about the wording; kept literal, this file is one of three
 * independent witnesses that a reword inside the shared module is deliberate.
 *
 * "Reopen <target>" is NOT one of those shared sentences and its assertions
 * must never be derived from `mutePresets`: it is this surface's own word for
 * lifting acknowledge, resolve and false-positive, and the
 * button that carries it is the same slot that says "Unmute" on a muted card.
 */
describe('AlertingInbox — the undo for a mute is called Unmute', () => {
  it('names the muted card\'s undo "Unmute", not "Reopen"', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup({ status: 'muted', muted: true, muted_until: '2026-08-19T10:00:00Z' })],
        total: 1,
      }),
    })

    // The word did not exist anywhere on this page: the control that lifts a
    // mute was labelled "Reopen", which does a different job on a resolved
    // card, while MonitorDetailPage had a literal Unmute for the other mute
    // system. Two vocabularies, one idea.
    expect(screen.getByRole('button', { name: `Unmute ${TARGET}` })).toBeInTheDocument()
    // Negative stays a prefix regex on purpose: the claim is that NO button on
    // this card starts with that verb, whatever it goes on to name.
    expect(screen.queryByRole('button', { name: /^Reopen / })).toBeNull()
    // …and the mute button becomes the way to CHANGE it, not a second silent
    // seven-day extension. "Change mute on" is this surface's own vocabulary —
    // no other mute surface can change a mute in place, so it is written here
    // and in the component as a literal rather than hosted in the shared module.
    expect(screen.getByRole('button', { name: `Change mute on ${TARGET}` })).toBeInTheDocument()
  })

  it('keeps the same slot named "Reopen" on a card that was never muted', () => {
    renderInbox({ inbox: makeInbox({ items: [makeGroup({ status: 'resolved' })], total: 1 }) })

    expect(screen.getByRole('button', { name: `Reopen ${TARGET}` })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Unmute / })).toBeNull()
  })

  it('offers the shared presets and reports the duration it will write', () => {
    const { onAction } = renderInbox()

    // Every mute used to be a hardcoded seven days with nothing on screen
    // saying so.
    fireEvent.click(screen.getByRole('button', { name: `Mute ${TARGET}` }))
    for (const label of ['1h', '24h', '7d']) {
      expect(screen.getByRole('button', { name: `Mute ${TARGET} for ${label}` }))
        .toBeInTheDocument()
    }

    // Named in full, not `/for 24h$/`: the old suffix regex asserted the
    // duration and said nothing about WHOSE incident was about to be silenced,
    // which is the half of the sentence that was added later.
    fireEvent.click(screen.getByRole('button', { name: `Mute ${TARGET} for 24h` }))
    expect(onAction).toHaveBeenCalledTimes(1)
    const variables = at(onAction.mock.calls, 0)[0]
    expect(variables.action).toBe('mute')
    // Resolved to an absolute future instant here, so the sentence the confirm
    // shows and the value that is written are the same one.
    expect(new Date(variables.mutedUntil!).getTime()).toBeGreaterThan(Date.now())
  })
})

describe('AlertingInbox — an incident can be silenced with no end date', () => {
  it('offers the open-ended choice beside the timed presets, and asks for null', () => {
    const { onAction } = renderInbox()

    fireEvent.click(screen.getByRole('button', { name: `Mute ${TARGET}` }))

    // Supplementing the presets, not replacing them: a snooze is still the
    // common case and must stay one click away.
    for (const label of ['1h', '24h', '7d']) {
      expect(screen.getByRole('button', { name: `Mute ${TARGET} for ${label}` }))
        .toBeInTheDocument()
    }
    // And only three of them — the open-ended choice is counted separately
    // below, so a fourth DURATION appearing here would be caught rather than
    // absorbed into "presets and an indefinite". Per surface, because each one
    // can grow a button of its own on top of whatever `MUTE_PRESETS` holds. A
    // predicate, not an interpolated RegExp: the target is a scope name that
    // came from the warehouse and may carry anything.
    const presetPrefix = `Mute ${TARGET} for `
    expect(
      screen.getAllByRole('button', { name: (name: string) => name.startsWith(presetPrefix) }),
    ).toHaveLength(3)
    const indefinite = screen.getByRole('button', {
      name: `Mute ${TARGET} until unmuted`,
    })
    // BOTH halves literal, and that is the point of this pair: it is the only
    // assertion in the repo that proves, against a real DOM node, that the
    // visible face and the accessible name of the open-ended button are
    // deliberately DIFFERENT strings — "for Until I unmute" is not English, so
    // `muteChoiceName` phrases that branch separately. Deriving either side
    // from `INDEFINITE_MUTE.label` or from the builder would degrade this to
    // "the constant equals the constant" and leave the asymmetry pinned nowhere
    // at any surface. It is also the one accepted WCAG 2.5.3 deviation in the
    // mute vocabulary: the visible phrase is not inside the name, so speech
    // input cannot activate this button by reading it aloud.
    expect(indefinite).toHaveTextContent('Until I unmute')

    fireEvent.click(indefinite)
    expect(onAction).toHaveBeenCalledTimes(1)
    const variables = at(onAction.mock.calls, 0)[0]
    expect(variables.action).toBe('mute')
    // STRICTLY null, and the key present. `undefined` is indistinguishable from
    // "no duration chosen" two hops downstream, where an object spread would
    // drop `muted_until` from the request body altogether.
    expect(variables.mutedUntil).toBeNull()
    expect('mutedUntil' in variables).toBe(true)
  })

  it('says on the card that a mute with no end date is in force', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup({ status: 'muted', muted: true, muted_until: null })],
        total: 1,
      }),
    })

    // The line used to be guarded on `muted && muted_until`, so an incident
    // silenced forever rendered a "Muted" chip and not one word about how long
    // for or how to undo it — the silent mute, back again.
    const card = document.getElementById('incident-grp-1')!
    expect(within(card).getByText(/no end date/i)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: `Unmute ${TARGET}` })).toBeInTheDocument()
  })

  it('still says nothing about a mute that has already lapsed', () => {
    // The backend reports a LAPSED mute as status open, muted false, and
    // `muted_until` NULLED — the same null the open-ended case carries, so
    // `muted` is the only signal separating them. Making the test above pass by
    // keying the line off `muted_until` brings back the card that showed an
    // "Open" badge and a mute line at once.
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup({ status: 'open', muted: false, muted_until: null })],
        total: 1,
      }),
    })

    const card = document.getElementById('incident-grp-1')!
    expect(within(card).queryByText(/^muted/i)).toBeNull()
    expect(within(card).getByText('Open')).toBeInTheDocument()
  })
})

describe('AlertingInbox — the card says what fired', () => {
  it('renders direction and scope kind, so two incidents on one scope are distinguishable', () => {
    renderInbox({
      inbox: makeInbox({
        items: [
          makeGroup(),
          makeGroup({
            correlation_group_id: 'grp-2',
            // A different scope here, so the only text under test is the two
            // reason chips — the cross-link below covers the shared-scope case.
            scope_ref: 'scope-b',
            scope_type: 'release_regression',
            scope_types: ['release_regression'],
          }),
        ],
        total: 2,
      }),
    })

    // Production's ranks 4 and 12: same event, same direction, same rule, same
    // scan — differing only in the axis the card never showed.
    expect(screen.getByText(/drop · volume/)).toBeInTheDocument()
    expect(screen.getByText(/drop · release regression/)).toBeInTheDocument()
  })

  it('cross-links two groups that share a scope', () => {
    renderInbox({
      inbox: makeInbox({
        items: [
          makeGroup(),
          makeGroup({
            correlation_group_id: 'grp-2',
            scope_types: ['release_regression'],
            scope_type: 'release_regression',
          }),
        ],
        total: 2,
      }),
    })

    // The answer to "I muted it and it came back for a different reason".
    const link = screen.getByRole('link', { name: /also here as drop · release regression/ })
    expect(link).toHaveAttribute('href', '#incident-grp-2')
  })

  it('states a zero baseline in words and never prints it as a percentage', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup({ actual_count: 412, expected_count: 0, percent_delta: null })],
        total: 1,
      }),
    })

    // The percent gate deliberately admits anomalies with no baseline, and the
    // stored delta for those used to be 0.0 — reporting the largest possible
    // relative move as the smallest one.
    expect(screen.getByText(/none expected · no baseline/)).toBeInTheDocument()
    expect(screen.queryByText(/0\.0%/)).toBeNull()
  })

  it('says a re-fired incident was already handled, and by whom', () => {
    renderInbox({
      inbox: makeInbox({
        items: [
          makeGroup({
            status: 'open',
            acted_at: '2026-07-30T12:00:00Z',
            acted_by: 'user-1',
            acted_by_name: 'V. Denisov',
          }),
        ],
        total: 1,
      }),
    })

    expect(screen.getByText(/Closed .* by V\. Denisov · firing again/)).toBeInTheDocument()
  })

  it('links each rule by its own id, not by position', () => {
    renderInbox({
      inbox: makeInbox({
        items: [
          makeGroup({
            rules: [
              { id: 'rule-b', name: 'Volume rule' },
              { id: 'rule-a', name: 'Regression rule' },
            ],
            rule_names: ['Regression rule', 'Volume rule'],
          }),
        ],
        total: 1,
      }),
    })

    // `rules` pairs id with name. The parallel arrays could not be zipped —
    // one came back sorted by UUID and the other by name.
    expect(screen.getByRole('link', { name: 'Volume rule' }))
      .toHaveAttribute('href', '/p/demo/monitors/rule-b')
    expect(screen.getByRole('link', { name: 'Regression rule' }))
      .toHaveAttribute('href', '/p/demo/monitors/rule-a')
  })
})

/**
 * A release regression's name was dead text on 30 of the 123
 * incidents on the live instance: the deny-set in lib/monitoring.ts refuses the
 * event page as SUBSTANTIATION for a release-cohort comparison, and this card
 * was using that one answer to decide whether there was anywhere to go at all.
 *
 * The deny-set is unchanged and these tests are written so it has to stay that
 * way — what the card gains is a second, separately-worded affordance that says
 * what it opens instead of pretending to prove anything. Both partitions the
 * detector emits get one, and they are different pages worded differently: the
 * event-scoped row opens the event, the event-TYPE-scoped row the event type.
 */
describe('AlertingInbox — a release regression can be navigated to, not substantiated', () => {
  /** The production shape: scope_ref IS the event id, and `event_id` repeats it. */
  function regression(overrides: Partial<AlertInboxGroup> = {}): AlertInboxGroup {
    return makeGroup({
      scope_type: 'release_regression',
      scope_types: ['release_regression'],
      scope_ref: 'evt-1',
      event_id: 'evt-1',
      ...overrides,
    })
  }

  it('leaves the name unlinked and offers a separate way to open the event', () => {
    renderInbox({ inbox: makeInbox({ items: [regression()], total: 1 }) })

    // The name itself stays plain text: clicking the identity line reads as
    // "here is the proof", and the event page cannot prove this comparison.
    const card = document.getElementById('incident-grp-1')!
    expect(card).toHaveTextContent(TARGET)
    expect(screen.queryByRole('link', { name: TARGET })).toBeNull()

    expect(
      screen.getByRole('link', { name: `View event volume for ${TARGET}` }),
    ).toHaveAttribute('href', '/p/demo/monitoring/event/evt-1')
  })

  it('names the chart it opens and carries the caveat the words have no room for', () => {
    renderInbox({ inbox: makeInbox({ items: [regression()], total: 1 }) })

    const link = screen.getByRole('link', { name: `View event volume for ${TARGET}` })
    // "view event volume", not "open event" or "details": the visible words
    // name the chart the reader lands on, which is what stops the click reading
    // as corroboration of the release-cohort comparison above it.
    expect(link).toHaveTextContent('view event volume')
    expect(link.getAttribute('title')).toContain('not the release-cohort comparison')
  })

  it('opens the event TYPE when the regression was found on one, in its own words', () => {
    // The detector's other partition, emitted on every scan with an app version
    // column: event_id is null and scope_ref is the event_type_id, which
    // /monitoring/event-type/:id is a real page for. Withholding a link here
    // used to be justified by "the row carries no event" — true of the EVENT
    // route and silent about this one, so the type's name stayed dead text
    // beside a page that renders it perfectly well.
    renderInbox({
      inbox: makeInbox({
        items: [regression({ scope_ref: 'et-1', event_id: null })],
        total: 1,
      }),
    })

    const card = document.getElementById('incident-grp-1')!
    expect(card).toHaveTextContent(TARGET)
    // Still not the name: this is navigation, and the deny-set's refusal to
    // substantiate a release-cohort comparison is untouched.
    expect(screen.queryByRole('link', { name: TARGET })).toBeNull()

    // Its OWN wording. "view event volume" on an event-type page would repeat
    // the event/event-type conflation the deny-set exists to stop, and the two
    // links have to stay tellable apart when both shapes sit in one inbox.
    const link = screen.getByRole('link', { name: `View event type volume for ${TARGET}` })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event-type/et-1')
    expect(link).toHaveTextContent('view event type volume')
    expect(link.getAttribute('title')).toContain("event type's own monitoring page")
  })

  it('adds no second link to a scope the card already links', () => {
    // The double-link guard from the card's side: an ordinary event group has
    // its name linked as substantiation and must not also grow a navigation
    // link to the same page. `getScopeNavigationTarget` is guarded by the
    // complement of `getScopeMonitoringPath` precisely so this cannot be
    // forgotten. The pattern spans both wordings so adding a third destination
    // cannot slip past this guard.
    renderInbox()

    expect(screen.getByRole('link', { name: TARGET })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/scope-a',
    )
    expect(screen.queryByRole('link', { name: /view event (type )?volume/i })).toBeNull()
  })
})

describe('AlertingInbox — action slots do not move between rows', () => {
  it('renders every slot on every row, disabling the inapplicable one', () => {
    renderInbox({
      inbox: makeInbox({
        items: [
          makeGroup(),
          makeGroup({ correlation_group_id: 'grp-2', status: 'resolved' }),
        ],
        total: 2,
      }),
    })

    // Before this, an open row read [Ack][Resolve][Mute][False positive] and a
    // resolved row read [Reopen] alone, so clicking down the list at a fixed x
    // turned a snooze into the destructive action.
    expect(screen.getAllByRole('button', { name: /^Acknowledge / })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: /^Resolve / })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: /^Reopen / })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: /^Mark .* as a false positive$/ })).toHaveLength(2)

    // Row 1 is open: Ack is live, Reopen is not. Row 2 is the mirror image.
    const acks = screen.getAllByRole('button', { name: /^Acknowledge / })
    const reopens = screen.getAllByRole('button', { name: /^Reopen / })
    expect(acks[0]).toBeEnabled()
    expect(reopens[0]).toBeDisabled()
    expect(acks[1]).toBeDisabled()
    expect(reopens[1]).toBeEnabled()
  })
})

describe('AlertingInbox — three states, three branches', () => {
  it('does not claim there are no incidents while it is still asking', () => {
    renderInbox({ inbox: undefined, isLoading: true })

    expect(screen.getByText('Loading incidents…')).toBeInTheDocument()
    expect(screen.queryByText(/No incidents in the last 30 days/)).toBeNull()
  })

  it('reports a failed request as a failure, not as an empty queue', () => {
    renderInbox({ inbox: undefined, isError: true, loadError: new Error('boom') })

    expect(screen.getByRole('alert')).toHaveTextContent(/Could not load the inbox: boom/)
    expect(screen.queryByText(/No incidents in the last 30 days/)).toBeNull()
  })

  it('names the filter when a filter is what emptied the list', () => {
    renderInbox({ inbox: makeInbox({ items: [], total: 0 }), statusFilter: 'muted' })

    // "No correlated alert groups" is a claim about the project; this is a
    // claim about the question, and it comes with the way back.
    expect(screen.getByText(/No muted incidents\./)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show all' })).toBeInTheDocument()
  })
})

describe('AlertingInbox — feedback lands on the row it belongs to', () => {
  it('disables only the acting row, and renders its error inside its own card', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup(), makeGroup({ correlation_group_id: 'grp-2' })],
        total: 2,
      }),
      pendingGroupIds: new Set(['grp-1']),
      actionErrors: new Map([
        ['grp-1', recordInboxActionFailure(makeGroup(), new Error('Only failed deliveries can be retried'))],
      ]),
    })

    const acks = screen.getAllByRole('button', { name: /^Acknowledge / })
    expect(acks[0]).toBeDisabled()
    // One click used to disable all ~80 buttons on the page.
    expect(acks[1]).toBeEnabled()

    // …and the error used to render once, below all twenty cards.
    const card = document.getElementById('incident-grp-1')
    expect(card?.querySelector('[role="alert"]')?.textContent).toContain(
      'Only failed deliveries can be retried',
    )
  })
})

describe('AlertingInbox — a row error retires once the incident moves on', () => {
  it('drops a failed Acknowledge once the live incident is resolved elsewhere', () => {
    // Ack on A failed while A was open. A colleague (or the bulk bar) then
    // resolved A; the refetched card must not keep saying "Could not …".
    const failure = recordInboxActionFailure(makeGroup({ status: 'open' }), new Error('ack failed'))

    renderInbox({
      inbox: makeInbox({ items: [makeGroup({ status: 'resolved' })], total: 1 }),
      actionErrors: new Map([['grp-1', failure]]),
    })
    expect(
      within(document.getElementById('incident-grp-1')!).queryByRole('alert'),
    ).toBeNull()
  })

  it('keeps it while the incident still looks as it did when the action failed', () => {
    const failure = recordInboxActionFailure(makeGroup({ status: 'open' }), new Error('ack failed'))
    renderInbox({
      inbox: makeInbox({ items: [makeGroup({ status: 'open' })], total: 1 }),
      actionErrors: new Map([['grp-1', failure]]),
    })
    expect(
      within(document.getElementById('incident-grp-1')!).getByRole('alert'),
    ).toHaveTextContent('ack failed')
  })
})

describe('AlertingInbox — the note is reachable without taking an action', () => {
  it('collapses behind "Add note" on a card nobody has written on', () => {
    renderInbox()

    // Twenty always-open inputs were the widest element in every card.
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))
    expect(screen.getByRole('textbox', { name: /^Note on onboarding/ })).toBeInTheDocument()
  })

  it('saves a note on its own, without taking an action first', () => {
    const { onAction } = renderInbox({ noteDraftStore: createNoteDraftStore({ 'grp-1': 'expected, we retired the screen' }) })

    // A note used to ride along on an action, so writing down WHY something
    // was a false positive meant first undoing the false positive. `note`
    // moves no status.
    fireEvent.click(screen.getByRole('button', { name: 'Save note' }))
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ action: 'note' }))
  })

  it('puts the note ahead of the actions in DOM order', () => {
    renderInbox()

    // The placeholder promised the text would be "sent with the next action"
    // while the actions came first in tab order, so a keyboard user pressed the
    // action and the note was never sent. `order` keeps the visual layout.
    const card = document.getElementById('incident-grp-1')!
    const noteControl = screen.getByRole('button', { name: 'Add note' })
    const ack = screen.getByRole('button', { name: /^Acknowledge / })
    const position = noteControl.compareDocumentPosition(ack)
    expect(card.contains(noteControl)).toBe(true)
    expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('AlertingInbox — writing the note is not the hard part', () => {
  const noteBox = () => screen.getByRole('textbox', { name: /^Note on onboarding/ })

  it('hands the caret to the box it just revealed', () => {
    // "Add note" used to cost two clicks and a hunt — reveal the box, then go
    // find it — which is most of what made writing one feel like paperwork.
    renderInbox()

    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))

    expect(noteBox()).toHaveFocus()
  })

  it('does not seize the caret from a card that merely has a note already', () => {
    // The editor also starts open on a card carrying a stored note or a
    // surviving draft, and the inbox is a 50-row list. A blanket `autoFocus`
    // would have every such card race for the caret on load and drop it into
    // whichever one React committed last — which is why focus is armed by the
    // click and not by the mount.
    renderInbox({ inbox: makeInbox({ items: [makeGroup({ note: 'ticket FOO-12' })], total: 1 }) })

    expect(noteBox()).toBeInTheDocument()
    expect(noteBox()).not.toHaveFocus()
  })

  it('saves on Ctrl+Enter without leaving the box', () => {
    const { onAction } = renderInbox({ noteDraftStore: createNoteDraftStore({ 'grp-1': 'expected, we retired the screen' }) })

    fireEvent.keyDown(noteBox(), { key: 'Enter', ctrlKey: true })

    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ action: 'note' }))
  })

  it('leaves a bare Enter to make paragraphs', () => {
    // The reason this is a textarea rather than the one-line input it was: a
    // note is prose that wraps, and 2000 characters through a 28px slot shows
    // about one line of them at a time. A bare Enter that submitted would put
    // the second paragraph out of reach.
    const { onAction } = renderInbox({ noteDraftStore: createNoteDraftStore({ 'grp-1': 'first line' }) })

    fireEvent.keyDown(noteBox(), { key: 'Enter' })

    expect(onAction).not.toHaveBeenCalled()
  })

  it('offers to delete a note that turned out to be wrong', () => {
    // Emptying the box is how the server has always deleted a note —
    // `state.note = note.strip() or None` — and it was the one gesture the
    // editor refused, so a wrong note was permanent unless somebody thought to
    // overwrite it with a correction. The button has to NAME the case, because
    // "Save note" over an empty box reads as a no-op.
    const { onAction } = renderInbox({
      inbox: makeInbox({ items: [makeGroup({ note: 'wrong, this was the ios release' })], total: 1 }),
      noteDraftStore: createNoteDraftStore({ 'grp-1': '' }),
    })

    const clear = screen.getByRole('button', { name: 'Clear note' })
    expect(clear).toBeEnabled()
    fireEvent.click(clear)

    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ action: 'note' }))
  })

  it('has nothing to offer on an empty box over an incident with no note', () => {
    // The third state, and the only one where the control is genuinely inert:
    // there is neither a note to write nor one to delete.
    renderInbox()
    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))

    expect(screen.getByRole('button', { name: 'Save note' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Clear note' })).toBeNull()
  })

  it('says nothing about length on a note anybody would actually write', () => {
    // A counter pinned to every card is noise: incident notes are a sentence,
    // and the cap is roughly a page and a half.
    renderInbox({ noteDraftStore: createNoteDraftStore({ 'grp-1': 'x'.repeat(1799) }) })

    expect(screen.queryByText(/characters left$/)).toBeNull()
  })

  it('warns before the box starts silently dropping keystrokes', () => {
    // `maxLength` does not warn, error or truncate visibly — it simply stops
    // accepting input, and somebody pasting a stack trace reads that as the page
    // having frozen.
    renderInbox({ noteDraftStore: createNoteDraftStore({ 'grp-1': 'x'.repeat(1800) }) })

    expect(screen.getByText('200 characters left')).toBeInTheDocument()
  })

  it('says what is happening once the box is full, not just that it is zero', () => {
    // "0 characters left" states the number without stating the consequence, and
    // the consequence is the entire reason the line exists.
    renderInbox({ noteDraftStore: createNoteDraftStore({ 'grp-1': 'x'.repeat(2000) }) })

    expect(
      screen.getByText('Full — further characters are not being accepted'),
    ).toBeInTheDocument()
  })
})

describe('AlertingInbox — the header says how much of the queue is on screen', () => {
  it('counts what is shown against the server total, and offers the rest', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup(), makeGroup({ correlation_group_id: 'grp-2' })],
        total: 57,
      }),
      hasMore: true,
    })

    // The header used to print 57 above a list of 20, with no control of any
    // kind — 37 incidents reachable by no means at all.
    expect(screen.getByText('Showing 2 of 57 · last 30 days + still silenced')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Load more (55 left)' })).toBeInTheDocument()
    expect(screen.getByText(/Of the 2 incidents loaded: 2 open · 0 handled/)).toBeInTheDocument()
  })

  it('stops claiming 30 days when the server could not reach back that far', () => {
    // The list is capped on delivery rows as well as by the window, and the cap
    // is applied before incidents are grouped — so a loud enough project gets a
    // shorter window with the oldest incidents simply gone. The page said "last
    // 30 days" regardless, which made an absent incident look like a handled
    // one.
    const truncatedAt = '2026-08-09T04:30:00Z'
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup(), makeGroup({ correlation_group_id: 'grp-2' })],
        total: 57,
        window_truncated_at: truncatedAt,
      }),
    })

    // Through `formatDateTime`, not a literal, so this does not depend on the
    // runner's locale or zone (as AlertDeliveryRow's timestamp test does).
    const start = formatDateTime(truncatedAt)
    expect(screen.getByText(`Showing 2 of 57 · since ${start} + still silenced`)).toBeInTheDocument()
    // The window clause is REPLACED, not joined: two answers on one line is the
    // failure the line exists to prevent. (The notice below still SAYS "the
    // last 30 days" — as the period that overflowed, which is the true claim.)
    expect(screen.queryByText(/last 30 days \+ still silenced/)).toBeNull()
    // …and the date alone reads as a setting somebody chose, so the list says
    // what it means for the incidents that fell off.
    expect(
      screen.getByRole('status'),
    ).toHaveTextContent(/more alerts in the last 30 days than the Inbox reads at once/)
  })

  it('says nothing about truncation when the whole window fitted', () => {
    // Which is every deployment measured so far: a permanent caveat about a
    // bound nobody is near is the noise that teaches people to skip this spot.
    renderInbox({ inbox: makeInbox({ items: [makeGroup()], total: 1 }) })

    expect(screen.getByText('Showing 1 of 1 · last 30 days + still silenced')).toBeInTheDocument()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('pins a deep-linked incident that is outside the loaded pages', () => {
    renderInbox({
      inbox: makeInbox({ items: [makeGroup()], total: 57 }),
      pinnedGroup: makeGroup({
        correlation_group_id: 'grp-old',
        scope_names: ['settings/choose_model'],
        scope_ref: 'scope-old',
      }),
    })

    expect(screen.getByText(/Linked from an alert/)).toBeInTheDocument()
    expect(document.getElementById('incident-grp-old')).not.toBeNull()
  })
})

describe('AlertingInbox — incidents can be picked for one decision', () => {
  /** A second incident, on a scope whose name is nothing like the first's. */
  const OTHER_TARGET = 'checkout_started'
  /*
   * How each checkbox is announced: the REASON, then the scope. `makeGroup` is a
   * drop on an event scope, so the reason is "drop · volume" for both incidents
   * and only the scope tells them apart here — while on one scope firing both
   * ways it is the reason that does, which is the case the scope alone could not
   * name. Literals, not `incidentReasonLabel`, so
   * these assert the sentence rather than re-running the helper that builds it.
   */
  const SELECT_TARGET = `Select drop · volume on ${TARGET}`
  const SELECT_OTHER_TARGET = `Select drop · volume on ${OTHER_TARGET}`
  const twoIncidents = makeInbox({
    items: [
      makeGroup(),
      makeGroup({
        correlation_group_id: 'grp-2',
        scope_ref: 'scope-b',
        scope_names: [OTHER_TARGET],
      }),
    ],
    total: 2,
  })

  it('names each checkbox by its incident: the reason, then the scope', () => {
    renderInbox({ inbox: twoIncidents })

    // Reason + `scopeSummary`, NOT the rule and not the position. "Select
    // incident 2 of 2" would be the one control on the card that cannot say
    // which incident it belongs to — and it is the control that decides what a
    // bulk mute silences. The scope alone is not enough either: direction and
    // signal kind are part of the correlation key, so one scope firing both ways
    // is two cards and two identically announced checkboxes.
    expect(screen.getByRole('checkbox', { name: SELECT_TARGET })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: SELECT_OTHER_TARGET })).toBeInTheDocument()
    // The cards stay cards. Turning the inbox into a table to get a selection
    // column would have cost the magnitude line, the sibling cross-links, the
    // note and the deliveries disclosure that live inside each card.
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('raises the id and the new state, and holds no selection of its own', () => {
    const { toggleIncidentSelected } = renderInbox({ inbox: twoIncidents })

    fireEvent.click(screen.getByRole('checkbox', { name: SELECT_OTHER_TARGET }))
    expect(toggleIncidentSelected).toHaveBeenCalledWith('grp-2', true)
    // Still unticked afterwards: the component renders the page's set and never
    // its own. That is what lets the page drop a selected id the moment a
    // filter change or a refetch stops rendering its row — a card holding its
    // own `useState` would keep the tick over a row the page had forgotten.
    expect(screen.getByRole('checkbox', { name: SELECT_OTHER_TARGET })).not.toBeChecked()
  })

  it('reflects the page-held selection, and asks to unpick an already-picked row', () => {
    const { toggleIncidentSelected } = renderInbox({
      inbox: twoIncidents,
      selectedIncidents: new Set(['grp-1']),
    })

    const picked = screen.getByRole('checkbox', { name: SELECT_TARGET })
    expect(picked).toBeChecked()
    expect(screen.getByRole('checkbox', { name: SELECT_OTHER_TARGET })).not.toBeChecked()

    fireEvent.click(picked)
    expect(toggleIncidentSelected).toHaveBeenCalledWith('grp-1', false)
  })

  /*
   * Building a selection was strictly one click per card: 50 ticks on 16px boxes
   * to act on one loaded page, against 2 clicks for the same sweep on the events
   * table, which has both a header select-all and a shift-click range.
   * Neither control here can reach a row that is not rendered, so
   * neither is the "select all N matching" `InboxBulkActionBar` refuses.
   */
  const SELECT_ALL_SHOWN = 'Select all 2 shown incidents'
  const threeIncidents = makeInbox({
    items: [
      makeGroup(),
      makeGroup({ correlation_group_id: 'grp-2', scope_ref: 'scope-b', scope_names: [OTHER_TARGET] }),
      makeGroup({ correlation_group_id: 'grp-3', scope_ref: 'scope-c', scope_names: ['third_event'] }),
    ],
    total: 3,
  })

  it('offers one control that picks every incident on screen, counted in its name', () => {
    const { setIncidentsSelected } = renderInbox({ inbox: twoIncidents })

    const selectAll = screen.getByRole('checkbox', { name: SELECT_ALL_SHOWN })
    expect(screen.getByText('Select all 2 shown')).toBeInTheDocument()

    fireEvent.click(selectAll)
    expect(setIncidentsSelected).toHaveBeenCalledWith(['grp-1', 'grp-2'], true)
  })

  it('withdraws the control when the panel shows an error instead of cards', () => {
    // The reachable case is a failed background refetch, not a cold failure:
    // the inbox polls, and TanStack v5 sets status 'error' while KEEPING the
    // last `data`. The body then renders only the error paragraph while `inbox`
    // still holds the loaded page — so a header control derived from `inbox`
    // read "Select all 2 shown" over an empty panel, and one click built a
    // selection the bulk bar would then act on sight-unseen.
    renderInbox({ inbox: twoIncidents, isError: true, loadError: new Error('boom') })

    expect(screen.getByRole('alert')).toHaveTextContent(/Could not load the inbox/)
    expect(screen.queryByRole('checkbox', { name: SELECT_ALL_SHOWN })).toBeNull()
    expect(screen.queryByText(/Select all \d+ shown/)).toBeNull()
  })

  it('reads mixed while only some of the page is picked', () => {
    renderInbox({ inbox: twoIncidents, selectedIncidents: new Set(['grp-1']) })

    expect(screen.getByRole('checkbox', { name: SELECT_ALL_SHOWN })).toHaveAttribute(
      'aria-checked',
      'mixed',
    )
  })

  it('clears the page when everything on it is already picked', () => {
    // A fully-selected page needs a one-click way back, or the bulk bar's Clear
    // is the only exit and the header box is a one-way door.
    const { setIncidentsSelected } = renderInbox({
      inbox: twoIncidents,
      selectedIncidents: new Set(['grp-1', 'grp-2']),
    })

    const selectAll = screen.getByRole('checkbox', { name: SELECT_ALL_SHOWN })
    expect(selectAll).toBeChecked()
    fireEvent.click(selectAll)
    expect(setIncidentsSelected).toHaveBeenCalledWith(['grp-1', 'grp-2'], false)
  })

  it('extends from the last box touched when shift is held', () => {
    const { toggleIncidentSelected, setIncidentsSelected } = renderInbox({
      inbox: threeIncidents,
    })

    // First click is an ordinary toggle — and the anchor for what follows.
    fireEvent.click(screen.getByRole('checkbox', { name: SELECT_TARGET }))
    expect(toggleIncidentSelected).toHaveBeenCalledWith('grp-1', true)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Select drop · volume on third_event' }), {
      shiftKey: true,
    })
    expect(setIncidentsSelected).toHaveBeenCalledWith(['grp-1', 'grp-2', 'grp-3'], true)
  })

  it('leaves a shift-click with no anchor as a plain single toggle', () => {
    // Nothing has been ticked yet, so there is no range to infer — guessing one
    // would select rows the operator never pointed at.
    const { toggleIncidentSelected, setIncidentsSelected } = renderInbox({
      inbox: threeIncidents,
    })

    fireEvent.click(screen.getByRole('checkbox', { name: SELECT_OTHER_TARGET }), {
      shiftKey: true,
    })
    expect(toggleIncidentSelected).toHaveBeenCalledWith('grp-2', true)
    expect(setIncidentsSelected).not.toHaveBeenCalled()
  })

  it('gives a viewer no checkbox to build a selection with', () => {
    renderInbox({ inbox: twoIncidents }, 'viewer')

    // Every inbox action is editor-only server-side, so a selection a viewer
    // can build is a selection nothing on the page will let them spend — the
    // same reasoning that removed the action row for them, and
    // the checkbox has to be inside the same gate or the bulk bar becomes
    // reachable by a reader who can do nothing with it.
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    // …and everything they came to read is still there.
    expect(screen.getByText(TARGET)).toBeInTheDocument()
  })
})

describe('AlertingInbox — viewer gating', () => {
  // Every inbox action is editor-only server-side, and this list used to render
  // five enabled buttons plus a note box on every card to a viewer whose every
  // click round-tripped to a 403.
  const ACTION_NAMES = [
    /^Acknowledge /,
    /^Resolve /,
    /^Mute /,
    /^Reopen /,
    /as a false positive$/,
  ]

  it('gives an editor the full action row', () => {
    renderInbox({}, 'member')

    // Present on every card, always — the fixed slots are the whole point of
    // the card layout. Reopen is the one that is DISABLED on an open incident
    // rather than missing, which is exactly the distinction being asserted.
    for (const name of ACTION_NAMES) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: /^Acknowledge / })).toBeEnabled()
    expect(screen.getByRole('button', { name: /^Reopen / })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Add note' })).toBeEnabled()
  })

  it('gives a viewer no action at all, enabled or otherwise', () => {
    renderInbox({}, 'viewer')

    for (const name of ACTION_NAMES) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.queryByRole('button', { name: 'Add note' })).toBeNull()
  })

  it('says why once for the section, not once per card', () => {
    renderInbox(
      {
        inbox: makeInbox({
          items: [makeGroup(), makeGroup({ correlation_group_id: 'grp-2' })],
          total: 2,
        }),
      },
      'viewer',
    )

    expect(screen.getAllByText(/you have the viewer role in this project/i)).toHaveLength(1)
  })

  it('leaves everything a viewer came to read', () => {
    renderInbox({}, 'viewer')

    // The point of the section for a viewer: what fired, how big it was, and
    // what was already decided about it.
    expect(screen.getByText('onboarding/reviews_carousel')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Volume rule' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /what was sent/ })).toBeEnabled()
  })
})

/**
 * an analyst working through "the events I already know are
 * fine" had one control over 180 incidents.
 */
describe('AlertingInbox — narrowing the list past its status', () => {
  it('offers the scope kinds by the same names the cards use', async () => {
    const { onFiltersChange } = renderInbox()

    // "volume", not "event": the chip on the card below says the former, and a
    // picker with its own vocabulary makes one column read as two things.
    // The design-system Select, like the Delivery log's filter bar (ALR-49).
    fireEvent.click(screen.getByRole('combobox', { name: /^Kind filter/ }))
    expect(await screen.findByRole('option', { name: 'release regression' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: 'volume' }))

    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ scopeType: 'event' }),
    )
  })

  it('states the window a date filter can reach, on the control itself', async () => {
    renderInbox()

    // The list is read over 30 days and then capped, so a date older than that
    // narrows nothing — a control that accepted one and answered "none" would
    // be describing the project rather than the page.
    // The range and its caveat are one chip now, opened on demand (AL-15).
    fireEvent.click(screen.getByRole('button', { name: 'Last fired filter: any' }))
    expect(screen.getByText(/Dates narrow the 30 days this list already covers/)).toBeInTheDocument()

    // The app's calendar rather than a native date input (AL-15): the bound
    // is a disabled day, not a `min` attribute.
    const earliest = earliestReachableDay(new Date())
    // A date-only string with a time and no zone parses as LOCAL midnight.
    const bound = new Date(`${earliest}T00:00:00`)
    fireEvent.click(screen.getByLabelText('Last fired from'))
    const grid = await screen.findByRole('grid')
    const now = new Date()
    if (bound.getMonth() !== now.getMonth()) {
      // It opens on today, clamped into range; the floor is last month.
      fireEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    }
    const dayName = (date: Date) =>
      date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
    expect(within(grid).getByRole('button', { name: dayName(bound) })).toBeEnabled()
    if (bound.getDate() > 1) {
      const before = new Date(bound.getFullYear(), bound.getMonth(), bound.getDate() - 1)
      expect(within(grid).getByRole('button', { name: dayName(before) })).toBeDisabled()
    }
  })

  it('names the filters in the empty state, and Show all clears them with the status', () => {
    const { onFiltersChange, ...utils } = renderInbox({
      inbox: makeInbox({ items: [], total: 0 }),
      filters: { ...EMPTY_INBOX_FILTERS, direction: 'drop' },
    })
    const onStatusFilterChange = vi.fn()
    utils.rerender(
      <AuthContext.Provider value={authValue('member')}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter>
            <AlertingInbox
              slug="demo"
              inbox={makeInbox({ items: [], total: 0 })}
              isLoading={false}
              isError={false}
              loadError={null}
              pinnedGroup={null}
              hasRules
              statusFilter=""
              onStatusFilterChange={onStatusFilterChange}
              filters={{ ...EMPTY_INBOX_FILTERS, direction: 'drop' }}
              onFiltersChange={onFiltersChange}
              onLoadMore={vi.fn()}
              hasMore={false}
              isLoadingMore={false}
              noteDraftStore={createNoteDraftStore()}
              expandedIncidents={new Set()}
              toggleIncident={vi.fn()}
              selectedIncidents={new Set()}
              toggleIncidentSelected={vi.fn()}
              setIncidentsSelected={vi.fn()}
              onAction={vi.fn()}
              pendingGroupIds={new Set()}
              actionErrors={new Map()}
              onGoToMonitors={vi.fn()}
            />
          </MemoryRouter>
        </QueryClientProvider>
      </AuthContext.Provider>,
    )

    const showAll = screen.getByRole('button', { name: 'Show all' })
    // Naming the filter is the difference between "nothing has ever happened
    // here" and "nothing matches what you asked for" — the second is undoable.
    expect(showAll.closest('div')?.textContent).toContain('No incidents match these filters.')

    fireEvent.click(showAll)
    // Both halves, because either can be the one that emptied the page.
    expect(onStatusFilterChange).toHaveBeenCalledWith('')
    expect(onFiltersChange).toHaveBeenCalledWith(EMPTY_INBOX_FILTERS)
  })
})

describe('AlertingInbox — each card keeps its own action state (ALR-28)', () => {
  const twoCards = () =>
    makeInbox({
      items: [
        makeGroup(),
        makeGroup({
          correlation_group_id: 'grp-2',
          scope_ref: 'scope-b',
          scope_names: ['checkout_started'],
        }),
      ],
      total: 2,
    })

  it('keeps both cards busy while both have an action in flight', () => {
    // One id meant acting on B re-enabled A while A's request was still out, so
    // a second click on A could go out behind the first.
    renderInbox({ inbox: twoCards(), pendingGroupIds: new Set(['grp-1', 'grp-2']) })

    for (const ack of screen.getAllByRole('button', { name: /^Acknowledge / })) {
      expect(ack).toBeDisabled()
    }
  })

  it('renders each failure on the card it belongs to', () => {
    // A's failure used to be attributed to whichever card acted last.
    renderInbox({
      inbox: twoCards(),
      actionErrors: new Map([
        ['grp-1', recordInboxActionFailure(twoCards().items[0]!, new Error('first failed'))],
        ['grp-2', recordInboxActionFailure(twoCards().items[1]!, new Error('second failed'))],
      ]),
    })

    expect(
      within(document.getElementById('incident-grp-1')!).getByRole('alert'),
    ).toHaveTextContent('first failed')
    expect(
      within(document.getElementById('incident-grp-2')!).getByRole('alert'),
    ).toHaveTextContent('second failed')
  })
})

describe('AlertingInbox — clearing cannot be undone by a pending scope search (ALR-50)', () => {
  const settleDebounce = () =>
    act(async () => {
      await new Promise(resolve => setTimeout(resolve, SEARCH_DEBOUNCE_MS * 2))
    })

  it('drops a scope typed inside the debounce window when Clear filters is pressed', async () => {
    const { onFiltersChange } = renderInbox({
      filters: { ...EMPTY_INBOX_FILTERS, direction: 'drop' },
    })

    fireEvent.change(screen.getByLabelText('Search scopes'), { target: { value: 'checkout' } })
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    await settleDebounce()

    expect(onFiltersChange).toHaveBeenCalledWith(EMPTY_INBOX_FILTERS)
    expect(onFiltersChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'checkout' }),
    )
    expect(screen.getByLabelText('Search scopes')).toHaveValue('')
  })

  it('drops it on Show all too, which clears from outside the bar', async () => {
    const { onFiltersChange } = renderInbox({
      inbox: makeInbox({ items: [], total: 0 }),
      filters: { ...EMPTY_INBOX_FILTERS, direction: 'drop' },
    })

    fireEvent.change(screen.getByLabelText('Search scopes'), { target: { value: 'checkout' } })
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    await settleDebounce()

    expect(onFiltersChange).toHaveBeenCalledWith(EMPTY_INBOX_FILTERS)
    expect(onFiltersChange).not.toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'checkout' }),
    )
    expect(screen.getByLabelText('Search scopes')).toHaveValue('')
  })
})

describe('AlertingInbox — the filter bar speaks the design system (ALR-49)', () => {
  it('offers direction through the same Select the Delivery log uses', async () => {
    const { onFiltersChange } = renderInbox()

    fireEvent.click(screen.getByRole('combobox', { name: /^Direction filter/ }))
    fireEvent.click(await screen.findByRole('option', { name: 'spike ↑' }))

    expect(onFiltersChange).toHaveBeenCalledWith(
      expect.objectContaining({ direction: 'spike' }),
    )
  })

  it('maps "any" back to no filter rather than to a sentinel', async () => {
    const { onFiltersChange } = renderInbox({
      filters: { ...EMPTY_INBOX_FILTERS, direction: 'drop' },
    })

    fireEvent.click(screen.getByRole('combobox', { name: /^Direction filter/ }))
    fireEvent.click(await screen.findByRole('option', { name: 'any' }))

    expect(onFiltersChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ direction: '' }),
    )
  })
})

describe('AlertingInbox — the card leads with what broke and by how much (AL-12, AL-17, AL-18)', () => {
  it('puts a signed delta badge beside the scope headline', () => {
    renderInbox()

    const card = document.getElementById('incident-grp-1')!
    // 412 against 1,010 expected, on a drop: a real minus sign, whole percent.
    expect(within(card).getByText('−59%')).toBeInTheDocument()
    expect(within(card).getByRole('link', { name: TARGET })).toBeInTheDocument()
  })

  it('shows no percentage badge where there was no baseline to be a percentage of', () => {
    renderInbox({
      inbox: makeInbox({
        items: [makeGroup({ direction: 'spike', actual_count: 412, expected_count: 0, percent_delta: null })],
      }),
    })

    expect(screen.queryByText(/%$/)).toBeNull()
  })

  it('names the action "Acknowledge", as the bulk bar does', () => {
    renderInbox()

    expect(screen.getByRole('button', { name: `Acknowledge ${TARGET}` })).toHaveTextContent(
      'Acknowledge',
    )
  })

  it('keeps the Reopen slot on an open card but does not draw it', () => {
    renderInbox()

    expect(screen.getByRole('button', { name: `Reopen ${TARGET}` })).toHaveClass('invisible')
  })

  it('offers a rule, not a sentence, when there are no rules', () => {
    const onGoToMonitors = vi.fn()
    renderInbox({ hasRules: false, onGoToMonitors })

    expect(screen.getByRole('heading', { name: 'No alert rules yet' })).toBeInTheDocument()
    expect(screen.getByText('0 incidents')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create a rule' }))
    expect(onGoToMonitors).toHaveBeenCalledTimes(1)
  })

  it('says "incidents", not "correlated alert groups", when the inbox is empty', () => {
    renderInbox({ inbox: makeInbox({ items: [], total: 0 }) })

    expect(
      screen.getByText('No incidents in the last 30 days. When a rule fires, it shows up here.'),
    ).toBeInTheDocument()
  })
})

describe('AlertingInbox — owners (F07, #260)', () => {
  const owners = [
    { user_id: 'u-1', name: 'anna' },
    { user_id: 'u-2', name: 'oleg' },
  ]

  it('names the owners and offers an editor "Notify owners"', () => {
    renderInbox({ inbox: makeInbox({ items: [makeGroup({ owners })] }) })

    expect(screen.getByText('Owners: @anna, @oleg')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: `Notify owners of ${TARGET} by email` }),
    ).toBeInTheDocument()
  })

  it('shows a viewer the owners without the action', () => {
    renderInbox({ inbox: makeInbox({ items: [makeGroup({ owners })] }) }, 'viewer')

    expect(screen.getByText('Owners: @anna, @oleg')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Notify owners/ })).toBeNull()
  })

  it('says nothing about owners on an unowned incident', () => {
    renderInbox()
    expect(screen.queryByText(/^Owners:/)).toBeNull()
  })
})
