import { describe, expect, it } from 'vitest'
import { buildNavGroups } from '../lib/navigation'
import { buildMetricBuildingBlocks, buildTourSteps } from './tourSteps'

describe('buildTourSteps', () => {
  const steps = buildTourSteps('acme')

  it('covers every core surface named in the acceptance', () => {
    const ids = steps.map((step) => step.id)
    for (const id of [
      'events',
      'scans',
      'live-activity',
      'metrics',
      'monitors',
      'anomalies',
      'coverage',
      'reconciliation',
      'branches',
      'alerting',
    ]) {
      expect(ids).toContain(id)
    }
  })

  it('deep-links each step to the real project surface', () => {
    const byId = new Map(steps.map((step) => [step.id, step.to]))
    expect(byId.get('events')).toBe('/p/acme/events')
    expect(byId.get('scans')).toBe('/p/acme/scans')
    expect(byId.get('live-activity')).toBe('/p/acme/overview')
    expect(byId.get('metrics')).toBe('/p/acme/metrics')
    // The Monitors SECTION of Alerting: the standalone page rendered the same
    // rules under a second noun and was merged in. `/monitors`
    // still resolves, but only through a redirect, and a tour step should land
    // on the real surface rather than bounce through one.
    expect(byId.get('monitors')).toBe('/p/acme/alerting?section=monitors')
    expect(byId.get('anomalies')).toBe('/p/acme/anomalies')
    expect(byId.get('coverage')).toBe('/p/acme/coverage')
    expect(byId.get('reconciliation')).toBe('/p/acme/reconciliation')
    expect(byId.get('branches')).toBe('/p/acme/branches')
    expect(byId.get('alerting')).toBe('/p/acme/alerting')
  })

  it('tags every step with the group its sidebar item is in (#251 JR-22)', () => {
    // ProductTour prints `step.area` as the step's chip. The scans step said
    // 'Connect', a group buildNavGroups has never produced, and Branches and
    // Alerting said Govern while the sidebar filed them under Plan and
    // Observe. Derived from navigation itself so a future move cannot drift.
    const groups = buildNavGroups('acme', undefined)
    const groupOf = (navId: string) =>
      groups.find((group) => group.items.some((item) => item.id === navId))?.label
    const byId = new Map(steps.map((step) => [step.id, step]))
    expect(byId.get('events')?.area).toBe(groupOf('events'))
    expect(byId.get('scans')?.area).toBe('Govern')
    expect(byId.get('branches')?.area).toBe(groupOf('branches'))
    expect(byId.get('branches')?.area).toBe('Plan')
    expect(byId.get('alerting')?.area).toBe('Observe')
    expect(byId.get('monitors')?.area).toBe(groupOf('alerting'))
    // Search is the command palette, not a sidebar item: no chip at all.
    expect(byId.get('search')?.area).toBeNull()
  })

  it('titles each page step with its sidebar label (#251 JR-22, SH-7)', () => {
    const labels = buildNavGroups('acme', undefined).flatMap((group) =>
      group.items.map((item) => item.label),
    )
    const byId = new Map(steps.map((step) => [step.id, step.title]))
    expect(byId.get('events')).toBe('Events')
    expect(byId.get('branches')).toBe('Plan branches')
    expect(byId.get('alerting')).toBe('Alerting')
    const pageSteps = [
      'events',
      'scans',
      'live-activity',
      'metrics',
      'anomalies',
      'coverage',
      'reconciliation',
      'branches',
      'alerting',
    ]
    for (const id of pageSteps) {
      expect(labels).toContain(byId.get(id))
    }
    // A section of a page keeps its own name.
    expect(byId.get('monitors')).toBe('Alert rules')
  })

  it('describes Coverage as plan implementation (#251 JR-22)', () => {
    const coverage = steps.find((step) => step.id === 'coverage')
    expect(coverage?.blurb).toMatch(/implemented/)
    expect(coverage?.blurb).not.toMatch(/platforms/)
  })

  it('describes a scan by what every run produces, not by a baseline', () => {
    // A scan fills the tracking plan; only a Catalog + monitoring scan records
    // metric points, and only on its schedule. "Pull recent volume from a source
    // so tripl can learn the baseline" described neither a Catalog only scan nor
    // the manual run a newcomer starts from this step.
    const scans = steps.find((step) => step.id === 'scans')
    expect(scans?.blurb).not.toMatch(/baseline/i)
    expect(scans?.blurb).toMatch(/tracking plan/i)
    expect(scans?.blurb).toMatch(/Catalog \+ monitoring/)
  })

  it('demos meaning-first search with the curated examples', () => {
    const search = steps.find((step) => step.id === 'search')
    expect(search).toBeDefined()
    expect(search?.blurb).toContain('Ctrl K')
    expect(search?.blurb).toContain('⌘K')
    expect(search?.blurb).toContain('purchase funnel')
    expect(search?.blurb).toContain('money back')
    expect(search?.to).toBe('/p/acme/overview')
  })
})

describe('buildMetricBuildingBlocks', () => {
  const blocks = buildMetricBuildingBlocks('acme')

  it('covers event volume, the three catalog kinds and fact tables', () => {
    const labels = blocks.map((block) => block.label)
    expect(labels).toEqual(
      expect.arrayContaining(['Event volume', 'Fact', 'SQL', 'Event composition']),
    )
    expect(labels).toContain('Fact tables')
  })

  it('deep-links every block to the surface that actually shows it', () => {
    // Every block used to point at a bare /p/acme/metrics, so the links existed
    // but discovered nothing: four of the five landed on the same unfiltered page.
    const byId = new Map(blocks.map((block) => [block.id, block.to]))

    // The three real MetricKind values open the catalog already filtered.
    expect(byId.get('fact')).toBe('/p/acme/metrics?kind=fact')
    expect(byId.get('sql')).toBe('/p/acme/metrics?kind=sql')
    expect(byId.get('event_composition')).toBe('/p/acme/metrics?kind=event_composition')

    // Event volume is not a catalog kind at all — it is the per-event series a
    // scan collects, which lives on the Events catalog.
    expect(byId.get('event-count')).toBe('/p/acme/events')

    expect(byId.get('fact-tables')).toBe('/p/acme/metrics/fact-tables')

    // No two blocks share a destination any more.
    const destinations = blocks.map((block) => block.to)
    expect(new Set(destinations).size).toBe(destinations.length)
  })
})
