import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useState, type ReactNode } from 'react'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import { DemoGuide, type DemoGuideProps } from './DemoGuide'
import { useGuideCardOpen } from './guideCardOpen'

const PHONE_QUERY = '(max-width: 639px)'
const COARSE_POINTER_QUERY = '(pointer: coarse)'

/**
 * Boxes from each element's `data-rect="top left width height"`; anything
 * without one is not laid out. jsdom's window is 1024×768.
 */
function stubLayout() {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: Element,
  ) {
    const [top = 0, left = 0, width = 0, height = 0] = (this.getAttribute('data-rect') ?? '')
      .split(' ')
      .map(Number)
    return {
      top,
      left,
      width,
      height,
      right: left + width,
      bottom: top + height,
      x: left,
      y: top,
      toJSON: () => ({}),
    } as DOMRect
  })
}

function stubMedia(...matching: string[]) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: matching.includes(query),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  )
}

/**
 * The Events page at the top of its scroll, in a column from x=272: the title
 * top-left, the toolbar top-right, and table rows filling the bottom, a name
 * link in each row's first cell.
 */
function EventsPage() {
  const rows = [590, 620, 650, 680, 710]
  return (
    <main id={MAIN_CONTENT_ID} data-rect="0 272 752 768">
      <h1 data-rect="70 280 200 30">Events</h1>
      {[700, 770, 840, 910].map((left) => (
        <button key={left} type="button" data-rect={`190 ${left} 60 28`}>
          Toolbar
        </button>
      ))}
      <table>
        <tbody>
          {rows.map((top) => (
            <tr key={top}>
              <td>
                <a href="/events/1" data-rect={`${top} 280 150 20`}>
                  Event
                </a>
              </td>
              {[450, 550, 650, 750, 850, 950].map((left) => (
                <td key={left} data-rect={`${top} ${left} 90 20`}>
                  cell
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  )
}

/** A phone's page: the heading at the top, a row of actions at the bottom. */
function PhonePage({ children }: { children?: ReactNode }) {
  return (
    <main id={MAIN_CONTENT_ID} data-rect="0 0 1024 768">
      <h1 data-rect="70 12 200 30">Reconciliation</h1>
      <button type="button" data-rect="700 20 100 30">
        Accept
      </button>
      <button type="button" data-rect="700 140 100 30">
        Dismiss
      </button>
      {children}
    </main>
  )
}

function guide(props: Partial<DemoGuideProps> = {}) {
  return (
    <DemoGuide
      stepKey="live-loop/run-scan"
      position={1}
      total={4}
      chapter="Run the live loop"
      title="Run a scan"
      instruction="Run a scan to pull fresh volume from the demo warehouse."
      cue="Click Run now."
      {...props}
    />
  )
}

const guideElement = () => document.querySelector<HTMLElement>('[data-demo-guide]')
/** The guide's face: its eyes, which blink. */
const mascot = () => guideElement()?.querySelector('.guide-eyes') ?? null

beforeEach(() => {
  stubLayout()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

describe('DemoGuide — where the card docks', () => {
  it('takes a bottom corner over a busy page, never the top over its title and toolbar', () => {
    // Counted corner by corner, the top-left (the title alone) weighed least.
    stubMedia()
    render(
      <>
        <EventsPage />
        {guide()}
      </>,
    )

    // Of the two bottom corners, the one clear of the rows' name links.
    expect(guideElement()).toHaveAttribute('data-guide-corner', 'bottom-right')
  })

  it('docks at the bottom of a phone, whatever the page holds there', () => {
    // The page's actions were busier than its heading, so the card went to
    // the top, over the heading, with nothing to scroll it out from under.
    stubMedia(PHONE_QUERY)
    render(
      <>
        <PhonePage />
        {guide()}
      </>,
    )

    expect(guideElement()).toHaveAttribute('data-guide-corner', 'bottom-left')
  })

  it('goes to the top of a phone only while the control is down there', () => {
    stubMedia(PHONE_QUERY)
    function WithControl() {
      const [control, setControl] = useState<HTMLElement | null>(null)
      return (
        <>
          <PhonePage>
            <button ref={setControl} type="button" data-rect="720 260 100 30">
              Run now
            </button>
          </PhonePage>
          {control && guide({ avoid: control })}
        </>
      )
    }
    render(<WithControl />)

    expect(guideElement()).toHaveAttribute('data-guide-corner', 'top-left')
  })
})

describe('DemoGuide — its size on a phone', () => {
  it('leaves its face out of the open card on a phone, and keeps it elsewhere', () => {
    stubMedia(PHONE_QUERY)
    const phone = render(
      <>
        <PhonePage />
        {guide()}
      </>,
    )
    expect(mascot()).toBeNull()
    phone.unmount()

    stubMedia()
    render(
      <>
        <EventsPage />
        {guide()}
      </>,
    )
    expect(mascot()).not.toBeNull()
  })
})

describe('DemoGuide — its words', () => {
  it('says tap on a touch screen', () => {
    stubMedia(COARSE_POINTER_QUERY)
    render(
      <>
        <EventsPage />
        {guide()}
      </>,
    )

    expect(screen.getByText('Tap Run now.')).toBeInTheDocument()
    expect(screen.queryByText('Click Run now.')).toBeNull()
  })

  it('says click for a mouse', () => {
    stubMedia()
    render(
      <>
        <EventsPage />
        {guide()}
      </>,
    )

    expect(screen.getByText('Click Run now.')).toBeInTheDocument()
  })
})

describe('DemoGuide — telling the strip its card is open', () => {
  function OpenProbe() {
    return <output data-testid="card-open">{String(useGuideCardOpen())}</output>
  }

  it('is open while the card shows, and not once it folds to its face', () => {
    stubMedia()
    const view = render(
      <>
        <EventsPage />
        {guide()}
        <OpenProbe />
      </>,
    )

    expect(screen.getByTestId('card-open')).toHaveTextContent('true')

    fireEvent.click(screen.getByRole('button', { name: 'Minimise the demo guide' }))
    expect(screen.getByTestId('card-open')).toHaveTextContent('false')

    fireEvent.click(screen.getByRole('button', { name: /^Show the demo guide/ }))
    expect(screen.getByTestId('card-open')).toHaveTextContent('true')

    view.rerender(
      <>
        <EventsPage />
        <OpenProbe />
      </>,
    )
    expect(screen.getByTestId('card-open')).toHaveTextContent('false')
  })

  it('is never open while hints are hidden: the guide is only its face', () => {
    stubMedia()
    render(
      <>
        <EventsPage />
        {guide({ onUnmute: () => {} })}
        <OpenProbe />
      </>,
    )

    expect(screen.getByTestId('card-open')).toHaveTextContent('false')
  })
})
