import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { OnboardingReturnBar } from './onboarding-return-bar'
import {
  buildOnboardingSteps,
  onboardingProgress,
  onboardingStepHref,
  parseOnboardingReturn,
} from './onboarding-steps'
import type { ProjectSummary } from '@/types'

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <OnboardingReturnBar />
    </MemoryRouter>,
  )
}

function makeSummary(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    event_type_count: 0,
    event_count: 0,
    active_event_count: 0,
    implemented_event_count: 0,
    review_pending_event_count: 0,
    archived_event_count: 0,
    variable_count: 0,
    scan_count: 0,
    alert_destination_count: 0,
    alert_rule_count: 0,
    monitoring_signal_count: 0,
    firing_monitor_count: 0,
    open_incident_count: 0,
    failing_scan_config_count: 0,
    latest_scan_job: null,
    latest_signal: null,
    ...overrides,
  }
}

describe('OnboardingReturnBar (#250)', () => {
  it('names the step and leads back to the checklist on the project home', () => {
    renderAt('/p/shop/scans?onboarding=scan&step=2-of-5')

    const bar = screen.getByRole('navigation', { name: 'Getting started' })
    expect(bar).toHaveTextContent('Step 2 of 5')
    expect(bar).toHaveTextContent('Run a catalog + monitoring scan')
    expect(screen.getByRole('link', { name: 'Back to checklist' })).toHaveAttribute(
      'href',
      '/p/shop/overview',
    )
  })

  it('finds the project from the tag on a page outside the project', () => {
    renderAt('/settings/data-sources?onboarding=source&step=1-of-4&project=shop')

    expect(screen.getByText('Step 1 of 4')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to checklist' })).toHaveAttribute(
      'href',
      '/p/shop/overview',
    )
  })

  it('renders nothing on a page not opened from the checklist', () => {
    const { container } = renderAt('/p/shop/scans')

    expect(container).toBeEmptyDOMElement()
  })
})

describe('onboarding steps', () => {
  it('falls back to the step order when the position tag is missing or bogus', () => {
    expect(parseOnboardingReturn('/p/shop/metrics/new', '?onboarding=metric&step=9-of-2')).toMatchObject({
      step: 'metric',
      number: 4,
      total: 5,
      slug: 'shop',
    })
    expect(parseOnboardingReturn('/p/shop/scans', '?onboarding=nope')).toBeNull()
    expect(parseOnboardingReturn('/settings/data-sources', '?onboarding=source')).toBeNull()
  })

  it('tags only links outside the project with the project slug', () => {
    expect(onboardingStepHref('/p/shop/scans', 'scan', 'shop')).toBe('/p/shop/scans?onboarding=scan')
    expect(onboardingStepHref('/settings/data-sources', 'source', 'shop')).toBe(
      '/settings/data-sources?onboarding=source&project=shop',
    )
  })

  it('reports the next step and progress the way the checklist counts them', () => {
    const steps = buildOnboardingSteps('shop', makeSummary(), 0, 0)

    expect(onboardingProgress(steps, true)).toMatchObject({ completed: 0, total: 5 })
    expect(onboardingProgress(steps, true).next?.id).toBe('source')
    // An editor cannot connect a source, so it is neither counted nor "next".
    expect(onboardingProgress(steps, false)).toMatchObject({ completed: 0, total: 4 })
    expect(onboardingProgress(steps, false).next?.id).toBe('scan')
  })
})
