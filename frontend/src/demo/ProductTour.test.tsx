import { act, render, screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CommandPaletteContext } from '@/components/command-palette-context'
import { ProductTour, TourDock } from './ProductTour'
import { buildTourSteps } from './tourSteps'
import { setWelcomeDismissed } from './welcomeDismissal'
import { expectNoAxeViolations } from '@/test/axe'

// Derived, not hardcoded: adding a tour step must not break these tests.
const TOTAL_STEPS = buildTourSteps('acme').length
const LAST_INDEX = TOTAL_STEPS - 1

function renderTour(onOpenChange: (open: boolean) => void = () => {}) {
  return render(
    <MemoryRouter>
      <ProductTour slug="acme" open onOpenChange={onOpenChange} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  window.localStorage.clear()
})

describe('ProductTour — progress survives the navigation it asks for', () => {
  it('advances and remembers the step when you open its surface', () => {
    // Every step deep-links to a real surface, so following the tour necessarily
    // closes the dialog. It used to reset to step one on close, which made the
    // sequence impossible to follow: going to step 1's surface and reopening put
    // you back on step 1 forever.
    const onOpenChange = vi.fn()
    const { unmount } = renderTour(onOpenChange)

    expect(screen.getByText(/^Step 1 of/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: /^open events$/i }))

    // Visiting the surface is progress, and it closes the dialog.
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('1')

    // Reopening (a fresh mount, as after navigating) resumes on the NEXT step.
    unmount()
    renderTour()
    expect(screen.getByText(/^Step 2 of \d+ ·/)).toBeInTheDocument()
  })

  it('keeps your place when Next is pressed', () => {
    renderTour()
    fireEvent.click(screen.getByRole('button', { name: /^next$/i }))
    expect(screen.getByText(/^Step 2 of \d+ ·/)).toBeInTheDocument()
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('1')
  })

  it.each([
    ['Escape', () => fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })],
    ['the close button', () => fireEvent.click(screen.getByRole('button', { name: /^close$/i }))],
  ])('keeps your place when the dialog is dismissed with %s', (_how, dismiss) => {
    const onOpenChange = vi.fn()
    const { unmount } = renderTour(onOpenChange)
    fireEvent.click(screen.getByRole('button', { name: /^next$/i }))

    dismiss()

    expect(onOpenChange).toHaveBeenCalledWith(false)
    unmount()
    renderTour()
    expect(screen.getByText(/^Step 2 of \d+ ·/)).toBeInTheDocument()
  })

  it('starts over once the tour is finished', () => {
    window.localStorage.setItem('tripl-tour:acme', String(LAST_INDEX))
    const { unmount } = renderTour()

    expect(screen.getByText(new RegExp(`^Step ${TOTAL_STEPS} of`))).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^finish$/i }))
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('0')

    unmount()
    renderTour()
    expect(screen.getByText(/^Step 1 of/)).toBeInTheDocument()
  })

  it('ignores a stored step that is out of range', () => {
    window.localStorage.setItem('tripl-tour:acme', '999')
    renderTour()
    expect(screen.getByText(/^Step 1 of/)).toBeInTheDocument()
  })
})

describe('ProductTour', () => {
  it('contains wide chapter content without creating a horizontal scroll gutter', () => {
    renderTour()

    expect(screen.getByRole('dialog')).toHaveClass('min-w-0', 'overflow-x-hidden')
  })

  it('opens on the first step and links it to the real surface', () => {
    renderTour()

    expect(
      screen.getByText(`Step 1 of ${TOTAL_STEPS} · a quick guided path through tripl.`),
    ).toBeInTheDocument()
    const open = screen.getByRole('link', { name: /^open events$/i })
    expect(open).toHaveAttribute('href', '/p/acme/events')
  })

  it('advances through the steps', () => {
    renderTour()
    fireEvent.click(screen.getByRole('button', { name: /^next$/i }))
    expect(screen.getByText(new RegExp(`^Step 2 of ${TOTAL_STEPS} ·`))).toBeInTheDocument()
  })

  it('exposes a direct index to every surface plus the metric building blocks', () => {
    renderTour()
    fireEvent.click(screen.getByRole('button', { name: 'All surfaces' }))

    // Every surface is one click away regardless of stepper position.
    expect(screen.getByRole('link', { name: /^Scans$/i })).toHaveAttribute(
      'href',
      '/p/acme/scans',
    )
    expect(screen.getByRole('link', { name: /^Plan branches$/i })).toHaveAttribute(
      'href',
      '/p/acme/branches',
    )

    // Each building block deep-links to the surface that actually shows it —
    // they used to share one bare /p/acme/metrics href.
    const href = (name: string) =>
      screen.getByRole('link', { name: new RegExp(`^${name}$`, 'i') }).getAttribute('href')

    expect(href('Fact tables')).toBe('/p/acme/metrics/fact-tables')
    expect(href('Fact')).toBe('/p/acme/metrics?kind=fact')
    expect(href('SQL')).toBe('/p/acme/metrics?kind=sql')
    expect(href('Event composition')).toBe('/p/acme/metrics?kind=event_composition')
    // Event volume is a scan-collected per-event series, not a catalog kind.
    expect(href('Event volume')).toBe('/p/acme/events')
  })

  it('offers a Finish action on the final step in place of Next', () => {
    renderTour()
    // Step to the end (N steps → N-1 advances).
    for (let i = 0; i < LAST_INDEX; i += 1) {
      fireEvent.click(screen.getByRole('button', { name: /^next$/i }))
    }
    expect(screen.queryByRole('button', { name: /^next$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /finish/i })).toBeInTheDocument()
  })
})

describe('ProductTour accessibility', () => {
  it('has no axe violations on the open dialog', async () => {
    renderTour()
    await expectNoAxeViolations(document.body)
  })
})

describe('ProductTour — the search step opens search (DEMO-18)', () => {
  it('opens the command palette instead of linking to the page the user is on', () => {
    window.localStorage.setItem('tripl-tour:acme', String(LAST_INDEX))
    const setOpen = vi.fn()
    const onOpenChange = vi.fn()
    render(
      <MemoryRouter>
        <CommandPaletteContext.Provider value={{ open: false, setOpen }}>
          <ProductTour slug="acme" open onOpenChange={onOpenChange} />
        </CommandPaletteContext.Provider>
      </MemoryRouter>,
    )

    // A button, not a link back to /overview.
    expect(screen.queryByRole('link', { name: /open search by meaning/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /open search by meaning/i }))

    expect(setOpen).toHaveBeenCalledWith(true)
    expect(onOpenChange).toHaveBeenCalledWith(false)
    // The last step: done, so the tour starts over next time.
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('0')
  })
})

describe('ProductTour — paging for keyboard and screen-reader users (DEMO-19)', () => {
  it('announces the step Next and Back land on', () => {
    const steps = buildTourSteps('acme')
    renderTour()

    fireEvent.click(screen.getByRole('button', { name: /^next$/i }))
    expect(screen.getByRole('status')).toHaveTextContent(
      `Step 2 of ${TOTAL_STEPS}: ${steps[1]?.title ?? ''}`,
    )

    fireEvent.click(screen.getByRole('button', { name: /^back$/i }))
    expect(screen.getByRole('status')).toHaveTextContent(
      `Step 1 of ${TOTAL_STEPS}: ${steps[0]?.title ?? ''}`,
    )
  })

  it('keeps Back focusable on the first step instead of disabling it', () => {
    renderTour()

    const back = screen.getByRole('button', { name: /^back$/i })
    expect(back).toBeEnabled()
    expect(back).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(back)
    expect(screen.getByText(/^Step 1 of/)).toBeInTheDocument()
  })

  it('turns Next into Finish on the same element, so focus is not dropped', () => {
    window.localStorage.setItem('tripl-tour:acme', String(LAST_INDEX - 1))
    renderTour()

    const next = screen.getByRole('button', { name: /^next$/i })
    next.focus()
    fireEvent.click(next)

    const finish = screen.getByRole('button', { name: /^finish$/i })
    expect(finish).toBe(next)
    expect(document.activeElement).toBe(finish)
  })
})

describe('ProductTour — the surface index (DEMO-20)', () => {
  it('keeps the index behind a disclosure until asked for', () => {
    renderTour()

    const toggle = screen.getByRole('button', { name: 'All surfaces' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('link', { name: /^Scans$/i })).toBeNull()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('link', { name: /^Scans$/i })).toBeInTheDocument()
  })

  it('lists each surface once and describes it without relying on title', () => {
    renderTour()
    fireEvent.click(screen.getByRole('button', { name: 'All surfaces' }))

    // "Search by meaning" is not a page; it used to be a second /overview link.
    expect(screen.queryByRole('link', { name: /^Search by meaning$/i })).toBeNull()
    const overviewLinks = screen
      .getAllByRole('link')
      .filter((link) => link.getAttribute('href') === '/p/acme/overview')
    expect(overviewLinks).toHaveLength(1)

    const scans = buildTourSteps('acme').find((step) => step.id === 'scans')
    expect(screen.getByRole('link', { name: /^Scans$/i })).toHaveAccessibleDescription(
      scans?.blurb ?? '',
    )
  })
})

describe('ProductTour — the welcome panel is its own choice (DEMO-26)', () => {
  it('offers the dismissed welcome panel back without restoring it on open', () => {
    act(() => {
      setWelcomeDismissed('acme', true)
    })
    renderTour()

    // Opening the tour left the dismissal alone…
    expect(window.localStorage.getItem('tripl-demo-welcome-dismissed:acme')).toBe('1')

    // …and the way back is one explicit click.
    fireEvent.click(screen.getByRole('button', { name: /show the welcome panel/i }))
    expect(window.localStorage.getItem('tripl-demo-welcome-dismissed:acme')).toBeNull()
  })

  it('does not offer the panel when it is not dismissed', () => {
    renderTour()

    expect(screen.queryByRole('button', { name: /show the welcome panel/i })).toBeNull()
  })
})

function Path() {
  return <span data-testid="path">{useLocation().pathname}</span>
}

/** The banner's arrangement: the dialog mounted while open, the dock always. */
function TourWithDock({ initiallyOpen = true }: { initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen)
  return (
    <>
      {open && <ProductTour slug="acme" open onOpenChange={setOpen} />}
      <TourDock slug="acme" onOpenTour={() => setOpen(true)} />
      <Path />
    </>
  )
}

function renderTourWithDock(options: { initiallyOpen?: boolean; palette?: () => void } = {}) {
  return render(
    <MemoryRouter initialEntries={['/p/acme/overview']}>
      <CommandPaletteContext.Provider value={{ open: false, setOpen: options.palette ?? (() => {}) }}>
        <TourWithDock initiallyOpen={options.initiallyOpen} />
      </CommandPaletteContext.Provider>
    </MemoryRouter>,
  )
}

const dock = () => screen.queryByRole('region', { name: 'Product tour' })

describe('ProductTour — the tour stays with you on the surface it opens (#251 JR-22)', () => {
  it('docks on the opened surface, and Next walks on to the following one', () => {
    const steps = buildTourSteps('acme')
    renderTourWithDock()
    expect(dock()).toBeNull()

    fireEvent.click(screen.getByRole('link', { name: /^open events$/i }))

    // The dialog stepped aside; the tour did not.
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('path').textContent).toBe('/p/acme/events')
    expect(dock()).toHaveTextContent(`step 1 of ${TOTAL_STEPS}`)
    expect(dock()).toHaveTextContent(steps[0]?.title ?? '')

    fireEvent.click(screen.getByRole('button', { name: `Next: ${steps[1]?.title ?? ''}` }))

    expect(screen.getByTestId('path').textContent).toBe(steps[1]?.to)
    expect(dock()).toHaveTextContent(`step 2 of ${TOTAL_STEPS}`)
    // The dialog's place moves with the dock: reopened, it waits on step 3.
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('2')
  })

  it('reopens the dialog from the dock', () => {
    renderTourWithDock()
    fireEvent.click(screen.getByRole('link', { name: /^open events$/i }))

    fireEvent.click(screen.getByRole('button', { name: 'All steps' }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/^Step 2 of/)).toBeInTheDocument()
  })

  it('closes without losing the place', () => {
    renderTourWithDock()
    fireEvent.click(screen.getByRole('link', { name: /^open events$/i }))

    fireEvent.click(screen.getByRole('button', { name: 'Close the tour' }))

    expect(dock()).toBeNull()
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('1')
  })

  it('opens search for the palette step, and finishes on the last', () => {
    const setOpen = vi.fn()
    // Docked one before the last step, as after opening that step's surface.
    window.localStorage.setItem('tripl-tour:acme', String(LAST_INDEX))
    renderTourWithDock({ palette: setOpen })
    fireEvent.click(screen.getByRole('button', { name: /^back$/i }))
    const beforeLast = buildTourSteps('acme')[LAST_INDEX - 1]
    fireEvent.click(screen.getByRole('link', { name: `Open ${beforeLast?.title ?? ''}` }))

    fireEvent.click(screen.getByRole('button', { name: /^Next: Search by meaning$/ }))

    expect(setOpen).toHaveBeenCalledWith(true)
    expect(dock()).toHaveTextContent(`step ${TOTAL_STEPS} of ${TOTAL_STEPS}`)
    expect(window.localStorage.getItem('tripl-tour:acme')).toBe('0')

    fireEvent.click(screen.getByRole('button', { name: 'Finish' }))
    expect(dock()).toBeNull()
  })

  it('shows nothing until a surface is opened from the tour', () => {
    renderTourWithDock({ initiallyOpen: false })
    expect(dock()).toBeNull()
  })

  it('has no axe violations while docked', async () => {
    renderTourWithDock()
    fireEvent.click(screen.getByRole('link', { name: /^open events$/i }))
    await expectNoAxeViolations(document.body)
  })
})
