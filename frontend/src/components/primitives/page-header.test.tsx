import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { PageBackLink, PageHeader } from './page-header'

// One header component instead of two "canonical" ones and a
// dozen hand-rolled h1s.
describe('PageHeader', () => {
  it('renders the title as the page heading under its eyebrow', () => {
    render(<PageHeader eyebrow="Observe" title="Events" />)
    expect(screen.getByRole('heading', { level: 1, name: 'Events' })).toBeInTheDocument()
    expect(screen.getByText('Observe')).toBeInTheDocument()
  })

  it('renders the back link, addon, description and actions', () => {
    render(
      <PageHeader
        back={<a href="/back">Back</a>}
        title="Metric"
        titleAddon={<span>Badge</span>}
        description="What this page is for."
        actions={<button type="button">Create</button>}
      />,
    )
    expect(screen.getByRole('link', { name: 'Back' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Metric' })).toBeInTheDocument()
    expect(screen.getByText('Badge')).toBeInTheDocument()
    expect(screen.getByText('What this page is for.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument()
  })

  it('leaves out the slots it was not given', () => {
    const { container } = render(<PageHeader title="Coverage" />)
    expect(screen.getByRole('heading', { level: 1, name: 'Coverage' })).toBeInTheDocument()
    expect(container.querySelector('p, button, a')).toBeNull()
  })

  // one h1 per page, whatever the slots; the stat row sits under
  // the title block, not in the actions slot.
  it('renders exactly one h1 and puts the stats row after the title block', () => {
    const { container } = render(
      <PageHeader
        eyebrow="Plan"
        title="Properties"
        description="Template placeholders used in event field values."
        actions={<button type="button">Add property</button>}
        stats={<span>Stat row</span>}
      />,
    )
    expect(container.querySelectorAll('h1')).toHaveLength(1)
    const stats = container.querySelector('[data-slot="page-stats"]')
    expect(stats).toHaveTextContent('Stat row')
    expect(container.firstElementChild?.lastElementChild).toBe(stats)
  })

  it('gives the title block the full row below sm, so the actions always wrap under it', () => {
    render(
      <PageHeader
        title="Scans"
        description="A long description that a phone should not squeeze beside a button."
        actions={<button type="button">New scan</button>}
      />,
    )
    const titleBlock = screen.getByRole('heading', { level: 1 }).parentElement?.parentElement
    expect(titleBlock).toHaveClass('basis-full', 'sm:basis-60')
  })

  it('renders no stats wrapper without stats', () => {
    const { container } = render(<PageHeader title="Scans" />)
    expect(container.querySelector('[data-slot="page-stats"]')).toBeNull()
  })
})

// One back link for every page: it names where it leads and draws the same
// chevron whether it is a link or a button.
describe('PageBackLink', () => {
  it('is a real link to the parent collection when given an address', () => {
    render(
      <MemoryRouter>
        <PageBackLink label="Properties" to="/p/demo/variables" />
      </MemoryRouter>,
    )
    const link = screen.getByRole('link', { name: 'Properties' })
    expect(link).toHaveAttribute('href', '/p/demo/variables')
    expect(link.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
  })

  it('is a button that calls back when the page decides where back is', () => {
    const onClick = vi.fn()
    render(<PageBackLink label="Metrics" onClick={onClick} />)
    const button = screen.getByRole('button', { name: 'Metrics' })
    expect(button).toHaveAttribute('type', 'button')
    fireEvent.click(button)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('draws the same icon either way', () => {
    const { container } = render(
      <MemoryRouter>
        <PageBackLink label="Event types" to="/p/demo/event-types" />
        <PageBackLink label="Events" onClick={() => {}} />
      </MemoryRouter>,
    )
    const icons = Array.from(container.querySelectorAll('svg')).map((svg) => svg.getAttribute('class'))
    expect(icons).toHaveLength(2)
    expect(icons[0]).toBe(icons[1])
  })
})
