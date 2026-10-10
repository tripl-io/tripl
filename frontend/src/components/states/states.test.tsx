import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import {
  ChartSkeleton,
  DisabledReason,
  EntityNotFound,
  PageSkeleton,
  QueryErrorState,
  ReadOnlyDefinition,
  ReadOnlyNotice,
  SectionSkeleton,
  StatValueSkeleton,
  disabledReasonAria,
  isNotFoundError,
} from '.'

describe('PageSkeleton', () => {
  it('is one labelled status region, with the bars hidden from assistive tech', () => {
    const { container } = render(<PageSkeleton label="Loading events…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading events…')
    const bars = container.querySelectorAll('[data-slot="skeleton"]')
    expect(bars.length).toBeGreaterThan(5)
    bars.forEach((bar) => expect(bar).toHaveAttribute('aria-hidden', 'true'))
  })

  it.each(['list', 'dashboard', 'detail', 'form', 'settings'] as const)('renders the %s shape', (variant) => {
    render(<PageSkeleton variant={variant} />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading page…')
  })

  it('does not print a "Loading" sentence where people can see it', () => {
    render(<PageSkeleton />)
    expect(screen.getByText('Loading page…')).toHaveClass('sr-only')
  })
})

describe('SectionSkeleton and ChartSkeleton', () => {
  it('labels a section', () => {
    render(<SectionSkeleton variant="table" label="Loading monitors…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading monitors…')
  })

  it('draws card-less rows for a table already inside a panel', () => {
    const { container } = render(<SectionSkeleton variant="rows" rows={3} label="Loading metrics…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading metrics…')
    expect(container.querySelectorAll('.border-b')).toHaveLength(3)
    expect(container.querySelector('.rounded-card')).toBeNull()
  })

  it('keeps the chart height so nothing jumps when it renders', () => {
    const { container } = render(<ChartSkeleton height={260} />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading chart…')
    expect(container.querySelector('[style*="height: 260px"]')).not.toBeNull()
  })
})

describe('StatValueSkeleton', () => {
  it('is an inline, silent placeholder for a KPI value', () => {
    const { container } = render(<StatValueSkeleton />)
    const el = container.firstElementChild
    expect(el?.tagName).toBe('SPAN')
    expect(el).toHaveAttribute('aria-hidden', 'true')
  })
})

describe('EntityNotFound / QueryErrorState', () => {
  it('recognises a 404', () => {
    expect(isNotFoundError(new ApiError('Not found', 404))).toBe(true)
    expect(isNotFoundError(new ApiError('Boom', 500))).toBe(false)
    expect(isNotFoundError(new Error('x'))).toBe(false)
  })

  it('offers the way back instead of a retry', () => {
    render(
      <MemoryRouter>
        <EntityNotFound title="Event not found" back={{ to: '/p/demo/events', label: 'Back to Events' }} />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { level: 2, name: 'Event not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to Events' })).toHaveAttribute('href', '/p/demo/events')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('renders not-found for a 404 and the retryable error otherwise', () => {
    const onRetry = vi.fn()
    const notFound = { title: 'Metric not found', back: { to: '/p/demo/metrics', label: 'Back to metrics' } }
    const { rerender } = render(
      <MemoryRouter>
        <QueryErrorState
          error={new ApiError('Not found', 404)}
          title="Could not load this metric"
          onRetry={onRetry}
          notFound={notFound}
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'Metric not found' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()

    rerender(
      <MemoryRouter>
        <QueryErrorState
          error={new ApiError('Server error', 500)}
          title="Could not load this metric"
          onRetry={onRetry}
          notFound={notFound}
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this metric')
    screen.getByRole('button', { name: 'Try again' }).click()
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})

describe('ReadOnlyNotice', () => {
  it('defaults to the viewer copy and takes an action', () => {
    render(<ReadOnlyNotice action={<a href="/settings/profile">Go to Profile</a>} />)
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent('Read-only: you have the viewer role in this project.')
    expect(screen.getByRole('link', { name: 'Go to Profile' })).toBeInTheDocument()
  })

  it('takes a narrower rule', () => {
    render(<ReadOnlyNotice>Only owners can change roles.</ReadOnlyNotice>)
    expect(screen.getByRole('note')).toHaveTextContent('Only owners can change roles.')
  })
})

describe('ReadOnlyDefinition', () => {
  it('pairs each label with its value and marks empty values', () => {
    render(
      <ReadOnlyDefinition
        items={[
          { label: 'Name', value: 'Orders' },
          { label: 'Unit', value: null },
        ]}
      />,
    )
    const terms = screen.getAllByRole('term').map((t) => t.textContent)
    const values = screen.getAllByRole('definition').map((d) => d.textContent)
    expect(terms).toEqual(['Name', 'Unit'])
    expect(values).toEqual(['Orders', '—'])
  })
})

describe('DisabledReason', () => {
  it('shows the reason as text the disabled button points at', () => {
    const reason = 'Add a data source first'
    render(
      <>
        <button type="button" disabled {...disabledReasonAria('new-scan', reason)}>
          New scan
        </button>
        <DisabledReason id="new-scan" reason={reason} />
      </>,
    )
    expect(screen.getByRole('button', { name: 'New scan' })).toHaveAccessibleDescription(reason)
  })

  it('renders nothing and describes nothing without a reason', () => {
    const { container } = render(<DisabledReason id="x" reason={null} />)
    expect(container).toBeEmptyDOMElement()
    expect(disabledReasonAria('x', null)).toEqual({})
  })
})

describe('ProjectNotFound', () => {
  it('keeps the brand as the way home and lists up to five projects to open', async () => {
    const { ProjectNotFound } = await import('./project-not-found')
    const projects = Array.from({ length: 7 }, (_, i) => ({
      id: `p${i}`,
      name: `Project ${i}`,
      slug: `project-${i}`,
    })) as unknown as Parameters<typeof ProjectNotFound>[0]['projects']
    render(
      <MemoryRouter>
        <ProjectNotFound slug="gone" projects={projects} />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'Project not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'tripl — home' })).toHaveAttribute('href', '/workspace')
    const list = screen.getByRole('navigation', { name: 'Your projects' })
    expect(list.querySelectorAll('a')).toHaveLength(5)
    expect(screen.getByRole('link', { name: /Project 0/ })).toHaveAttribute('href', '/p/project-0/overview')
  })
})
