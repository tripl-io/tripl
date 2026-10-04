import type { ReactNode } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import ShortcutsDialog from './shortcuts-dialog'
import { GO_TO_SHORTCUTS, GO_TO_TIMEOUT_MS, findCreateAction, useShellShortcuts } from './shell-shortcuts'

function Harness({ onOpenHelp, children }: { onOpenHelp: () => void; children: ReactNode }) {
  useShellShortcuts({ onOpenHelp })
  const location = useLocation()
  return (
    <main id={MAIN_CONTENT_ID}>
      <span data-testid="path">{location.pathname}</span>
      {children}
    </main>
  )
}

/** Mounted the way Layout is: a route element that sees `:slug`. */
function renderPage(children: ReactNode, initialPath = '/p/shop/overview') {
  const onOpenHelp = vi.fn()
  const page = <Harness onOpenHelp={onOpenHelp}>{children}</Harness>
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/p/:slug/*" element={page} />
        <Route path="*" element={page} />
      </Routes>
    </MemoryRouter>,
  )
  return { onOpenHelp }
}

const path = () => screen.getByTestId('path').textContent

afterEach(() => {
  vi.useRealTimers()
})

describe('shell shortcuts', () => {
  it('presses the page\'s "New …" button on c', () => {
    const create = vi.fn()
    renderPage(
      <>
        <button type="button">Export</button>
        <button type="button" onClick={create}>New event</button>
      </>,
    )
    fireEvent.keyDown(document.body, { key: 'c' })
    expect(create).toHaveBeenCalledTimes(1)
  })

  it('prefers a control marked data-create-action', () => {
    const marked = vi.fn()
    const named = vi.fn()
    renderPage(
      <>
        <button type="button" onClick={named}>New thing</button>
        <button type="button" data-create-action="" onClick={marked}>Add rule</button>
      </>,
    )
    fireEvent.keyDown(document.body, { key: 'c' })
    expect(marked).toHaveBeenCalledTimes(1)
    expect(named).not.toHaveBeenCalled()
  })

  it('leaves letters alone while typing, with a modifier, or under a dialog', () => {
    const create = vi.fn()
    renderPage(
      <>
        <input aria-label="Name" />
        <button type="button" onClick={create}>New event</button>
      </>,
    )
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Name' }), { key: 'c' })
    fireEvent.keyDown(document.body, { key: 'c', ctrlKey: true })
    fireEvent.keyDown(document.body, { key: 'c', metaKey: true })
    expect(create).not.toHaveBeenCalled()

    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    fireEvent.keyDown(document.body, { key: 'c' })
    dialog.remove()
    expect(create).not.toHaveBeenCalled()
  })

  it('skips a disabled or hidden create control', () => {
    const root = document.createElement('div')
    root.innerHTML =
      '<button disabled>New a</button><div hidden><button>New b</button></div><a href="/x">New c</a>'
    expect(findCreateAction(root)?.textContent).toBe('New c')
    expect(findCreateAction(null)).toBeNull()
  })

  it('opens the shortcut sheet on ?', () => {
    const { onOpenHelp } = renderPage(<p>Page</p>)
    fireEvent.keyDown(document.body, { key: '?', shiftKey: true })
    expect(onOpenHelp).toHaveBeenCalledTimes(1)
  })
})

describe('go-to sequences', () => {
  it('goes to a project page on g then its letter', () => {
    renderPage(<p>Page</p>)
    fireEvent.keyDown(document.body, { key: 'g' })
    fireEvent.keyDown(document.body, { key: 'e' })
    expect(path()).toBe('/p/shop/events')
    fireEvent.keyDown(document.body, { key: 'g' })
    fireEvent.keyDown(document.body, { key: 'a' })
    expect(path()).toBe('/p/shop/alerting')
  })

  it('ends the sequence on a c after g and reads the c as the create shortcut', () => {
    const create = vi.fn()
    renderPage(<button type="button" onClick={create}>New event</button>)
    fireEvent.keyDown(document.body, { key: 'g' })
    // Not a go-to letter: the sequence ends and the key is read on its own.
    fireEvent.keyDown(document.body, { key: 'c' })
    expect(create).toHaveBeenCalledTimes(1)
    expect(path()).toBe('/p/shop/overview')
    // The sequence is over, so a lone e goes nowhere.
    fireEvent.keyDown(document.body, { key: 'e' })
    expect(path()).toBe('/p/shop/overview')
  })

  it('times out when the second key comes late', () => {
    vi.useFakeTimers()
    renderPage(<p>Page</p>)
    fireEvent.keyDown(document.body, { key: 'g' })
    vi.advanceTimersByTime(GO_TO_TIMEOUT_MS + 1)
    fireEvent.keyDown(document.body, { key: 'e' })
    expect(path()).toBe('/p/shop/overview')
  })

  it('ignores the sequence while typing or under a dialog or menu', () => {
    renderPage(<input aria-label="Name" />)
    const input = screen.getByRole('textbox', { name: 'Name' })
    fireEvent.keyDown(input, { key: 'g' })
    fireEvent.keyDown(input, { key: 'e' })
    expect(path()).toBe('/p/shop/overview')

    // g typed on the page, then a menu opens: the letter belongs to the menu.
    fireEvent.keyDown(document.body, { key: 'g' })
    const menu = document.createElement('div')
    menu.setAttribute('role', 'menu')
    document.body.appendChild(menu)
    fireEvent.keyDown(document.body, { key: 'e' })
    menu.remove()
    expect(path()).toBe('/p/shop/overview')
    // And the interrupted sequence does not linger past it.
    fireEvent.keyDown(document.body, { key: 'e' })
    expect(path()).toBe('/p/shop/overview')
  })

  it('offers nothing outside a project', () => {
    renderPage(<p>Workspace</p>, '/workspace')
    fireEvent.keyDown(document.body, { key: 'g' })
    fireEvent.keyDown(document.body, { key: 'e' })
    expect(path()).toBe('/workspace')
  })

  it('points every letter at a distinct page', () => {
    const keys = GO_TO_SHORTCUTS.map((shortcut) => shortcut.key)
    expect(new Set(keys).size).toBe(keys.length)
    // Never a key that already means something on its own.
    expect(keys).not.toContain('c')
    expect(keys).not.toContain('g')
  })
})

describe('ShortcutsDialog', () => {
  it('lists every key the app answers to', () => {
    render(<ShortcutsDialog open onOpenChange={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })
    for (const text of [
      'Search or jump to a page',
      'Search the list on this page',
      'Create on this page (New event, New metric…)',
      'Show these shortcuts',
    ]) {
      expect(dialog).toHaveTextContent(text)
    }
    for (const { label } of GO_TO_SHORTCUTS) {
      expect(dialog).toHaveTextContent(label)
    }
    expect(dialog).toHaveTextContent(/Events\s*G\s*then\s*E/)
  })
})
