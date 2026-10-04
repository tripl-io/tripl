import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ThemeProvider } from './theme-provider'
import { TweaksPanelProvider } from './tweaks-panel'
import { useTweaksPanel } from './tweaks-panel-context'

function Opener() {
  const tweaks = useTweaksPanel()
  return (
    <button type="button" onClick={(event) => tweaks.setOpen(!tweaks.open, event.currentTarget)}>
      Appearance
    </button>
  )
}

/** Radix registers its outside-press listener a tick after the popover mounts. */
async function settle() {
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
}

function renderPanel() {
  return render(
    <ThemeProvider defaultTheme="dark" storageKey="tripl-ui-theme">
      <TweaksPanelProvider>
        <Opener />
      </TweaksPanelProvider>
    </ThemeProvider>,
  )
}

/** A controllable `prefers-color-scheme: dark` query. */
function installColorScheme(initialDark: boolean) {
  let matches = initialDark
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      get matches() {
        return matches
      },
      media: query,
      onchange: null,
      addEventListener: (_: string, cb: (event: MediaQueryListEvent) => void) => listeners.add(cb),
      removeEventListener: (_: string, cb: (event: MediaQueryListEvent) => void) =>
        listeners.delete(cb),
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
  return (dark: boolean) => {
    matches = dark
    for (const cb of listeners) cb({ matches: dark } as MediaQueryListEvent)
  }
}

beforeEach(() => {
  localStorage.clear()
  document.documentElement.className = ''
})

afterEach(() => {
  document.documentElement.className = ''
})

describe('TweaksPanel', () => {
  it('renders no floating trigger over the page', () => {
    renderPanel()
    expect(screen.queryByRole('button', { name: 'Open tweaks panel' })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('moves focus in, closes on Escape and hands focus back', async () => {
    renderPanel()
    const opener = screen.getByRole('button', { name: 'Appearance' })
    opener.focus()
    fireEvent.click(opener)

    const dialog = await screen.findByRole('dialog', { name: 'Appearance' })
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    expect(dialog.contains(document.activeElement)).toBe(true)

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
  })

  it('closes on a click outside', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Appearance' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await settle()

    // Radix's Popover defers an outside dismissal from a primary-button
    // pointerdown to the click that completes the press, so press the way a
    // user does: pointerdown, then click. The pointerdown alone keeps it open.
    fireEvent.pointerDown(document.body)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(document.body)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('is a popover hung from its trigger, not a panel fixed to the corner', async () => {
    renderPanel()
    const opener = screen.getByRole('button', { name: 'Appearance' })
    fireEvent.click(opener)
    const dialog = await screen.findByRole('dialog', { name: 'Appearance' })
    expect(dialog).toHaveAttribute('data-slot', 'popover-content')
    expect(dialog.className).not.toMatch(/\bfixed\b/)
    await settle()

    // A press on the trigger toggles the panel instead of closing and
    // reopening it on the same click.
    fireEvent.pointerDown(opener)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(opener)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('reports the selected options with aria-pressed', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Appearance' }))

    const violet = await screen.findByRole('button', { name: 'Violet' })
    expect(violet).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(violet)
    expect(violet).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Teal' })).toHaveAttribute('aria-pressed', 'false')

    const density = screen.getByRole('group', { name: 'Density' })
    // The shared SegmentedControl, not a hand-rolled copy.
    expect(density).toHaveAttribute('data-slot', 'segmented-control')
    expect(density.querySelector('[aria-pressed="true"]')).toHaveTextContent('Compact')
  })

  it('previews each accent through its own class and names the choice', async () => {
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Appearance' }))

    const accents = await screen.findByRole('group', { name: 'Accent' })
    const swatches = Array.from(accents.querySelectorAll('button'))
    expect(swatches.map((swatch) => swatch.getAttribute('aria-label'))).toEqual([
      'Teal',
      'Violet',
      'Lime',
      'Indigo',
      'Magenta',
    ])
    for (const swatch of swatches) {
      const label = swatch.getAttribute('aria-label')!.toLowerCase()
      expect(swatch).toHaveClass(`accent-${label}`)
    }
    expect(screen.getByText('Teal')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Indigo' }))
    expect(document.documentElement).toHaveClass('accent-indigo')
    expect(screen.getByText('Indigo')).toBeInTheDocument()
    expect(screen.queryByText('Teal')).toBeNull()
  })

  it('offers System, and a System theme follows the OS as it changes', async () => {
    const setOsDark = installColorScheme(false)
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Appearance' }))

    const system = await screen.findByRole('button', { name: 'System' })
    fireEvent.click(system)
    expect(system).toHaveAttribute('aria-pressed', 'true')
    expect(localStorage.getItem('tripl-ui-theme')).toBe('system')
    expect(document.documentElement).toHaveClass('light')

    act(() => setOsDark(true))
    expect(document.documentElement).toHaveClass('dark')
    expect(document.documentElement).not.toHaveClass('light')
  })
})
