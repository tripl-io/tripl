import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import ConceptsPage from './ConceptsPage'
import { PRODUCT_PILLARS } from '@/components/workspace-welcome-pillars'

function renderConcepts() {
  return render(
    <MemoryRouter initialEntries={['/p/demo/concepts']}>
      <Routes>
        <Route path="/p/:slug/concepts" element={<ConceptsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ConceptsPage', () => {
  it('frames the model with the three Plan / Observe / Govern areas', () => {
    renderConcepts()

    expect(screen.getByRole('heading', { name: 'Concepts' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: /How tripl models your plan/ }),
    ).toBeInTheDocument()

    // Each area is named in the concept map and again as a glossary group, so it
    // must appear at least twice.
    for (const area of ['Plan', 'Observe', 'Govern']) {
      expect(screen.getAllByText(area).length).toBeGreaterThanOrEqual(2)
    }
  })

  it('defines several key terms in the glossary', () => {
    renderConcepts()

    const keyTerms = [
      'Events',
      'Event types',
      'Meta fields',
      'Properties',
      'Relations',
      'Alert rules',
      'Signals',
      // The sidebar has an "Anomalies" surface; the glossary that claims to be
      // the naming authority has to define it.
      'Anomalies',
      'Scopes',
      'Reconciliation',
      'Shadow events',
      'Dead events',
    ]
    for (const term of keyTerms) {
      expect(
        screen.getByRole('heading', { name: term, level: 4 }),
      ).toBeInTheDocument()
    }
  })

  it('ships finished copy — no markdown left in any definition', () => {
    const { container } = renderConcepts()

    // TermRow renders `definition` as a bare text node with no markdown parsing
    // anywhere in the path, so a backtick in the AREAS constant reaches the
    // rendered page as a grave accent. The Events entry shipped
    // "such as `payment_failed`" that way, on the one page whose job is to be
    // the authority on the product's vocabulary.
    expect(container.textContent).not.toContain('`')
  })

  it('does not claim an alert rule raises signals (#238)', () => {
    renderConcepts()

    // The product raises signals from detection on every scan — a project with
    // zero alert rules still shows open signals — so the glossary must not make
    // a rule the cause of a signal. One name for the object: "monitor" and
    // "alert rule" no longer need a sentence explaining they are the same.
    expect(screen.queryByText(/open anomaly raised by a monitor/i)).toBeNull()
    expect(screen.getByText(/no alert rule has to exist for one to appear/i)).toBeInTheDocument()
    expect(screen.getByText(/an alert rule decides which signals matter/i)).toBeInTheDocument()
    expect(screen.queryByText(/name the same object/i)).toBeNull()
  })

  it('defines the nav terms it used to miss, each with an anchor (#238)', () => {
    const { container } = renderConcepts()
    for (const term of [
      'Overview',
      'Metrics',
      'Metric points',
      'Fact tables',
      'Coverage',
      'Data sources',
      'Plan history',
      'Detection settings',
      'Incidents',
      'In review',
      'Catalog and monitoring scans',
    ]) {
      expect(screen.getByRole('heading', { name: term, level: 4 })).toBeInTheDocument()
    }
    expect(container.querySelector('#term-metric-points')).not.toBeNull()
    expect(screen.getByRole('link', { name: 'Open Data sources in the app' })).toHaveAttribute(
      'href',
      '/settings/data-sources',
    )
  })

  it('makes each concept-map chip a jump to its glossary row (#238)', () => {
    const { container } = renderConcepts()
    const chip = container.querySelector('a[href="#term-shadow-events"]')
    expect(chip).toHaveTextContent('Shadow events')
    expect(container.querySelector('#term-shadow-events')).not.toBeNull()
  })

  it('teaches the scan chain in the glossary, not only on the scan screens', () => {
    renderConcepts()

    expect(screen.getByRole('heading', { name: 'Scans', level: 4 })).toBeInTheDocument()

    // Someone who gets a Telegram alert naming a scan comes here to find out
    // what a scan is. The definition has to reach the thing that messaged them,
    // in the same words the scans list and the scan form use.
    const scans = screen.getByText(/Warehouse queries that add events and fields/)
    expect(scans).toHaveTextContent(/metric points/)
    expect(scans).toHaveTextContent(/anomaly detection and alerts are built on/)

    // ...and it must not call every scan "scheduled" — a catalog-only scan has
    // no schedule, which is the very thing that makes it catalog-only.
    expect(screen.queryByText(/^Scheduled warehouse queries/)).toBeNull()
  })

  it('links a term to where it lives in the app, scoped to the project slug', () => {
    renderConcepts()

    expect(screen.getByRole('link', { name: 'Open Events in the app' })).toHaveAttribute(
      'href',
      '/p/demo/events',
    )
    expect(
      screen.getByRole('link', { name: 'Open Reconciliation in the app' }),
    ).toHaveAttribute('href', '/p/demo/reconciliation')
    // The rules section of Alerting, not the standalone page: that page
    // rendered the same alert rules under a second noun and was merged in.
    expect(screen.getByRole('link', { name: 'Open Alerting, for Alert rules' })).toHaveAttribute(
      'href',
      '/p/demo/alerting?section=monitors',
    )
  })

  it('starts every link name with the text the link shows', () => {
    renderConcepts()

    // A term that only surfaces somewhere is labelled with that place, as
    // "Open <page>" like every other row (#238). Its name used to be
    // "Open Signals in the app" on a link reading "Anomalies", so "click
    // Anomalies" never reached it.
    const signals = screen.getByRole('link', { name: 'Open Anomalies, for Signals' })
    expect(signals).toHaveAttribute('href', '/p/demo/anomalies')
    for (const link of screen.getAllByRole('link')) {
      const visible = link.firstChild?.textContent ?? ''
      expect(link).toHaveAccessibleName(expect.stringMatching(new RegExp(`^${visible}`)))
    }
  })

  it('names the three areas the way the welcome screen does', () => {
    renderConcepts()

    for (const pillar of Object.values(PRODUCT_PILLARS)) {
      expect(screen.getAllByText(pillar.tagline).length).toBeGreaterThan(0)
    }
  })
})
