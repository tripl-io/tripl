import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SETTINGS_CONTENT_ID } from './landmarks'
import { visibleGroupsAll } from './nav'
import { SettingsCommandPalette } from './settings-palette'

const PROJECTS = [
  { name: 'Acme Android', slug: 'acme-android' },
  { name: 'Acme iOS', slug: 'acme-ios' },
]

function renderPalette(
  overrides: { isOwner?: boolean; isPlatformAdmin?: boolean; publicDemo?: boolean; backHref?: string } = {},
) {
  const onLeave = vi.fn()
  const onSignOut = vi.fn()
  render(
    <>
      {/* The takeover's own content landmark — the palette's focus-restore
          target, since /settings/* never renders the shell's #main-content. */}
      <main id={SETTINGS_CONTENT_ID} tabIndex={-1}>
        section
      </main>
      <button type="button" data-testid="opener">
        Open
      </button>
      <SettingsCommandPalette
        activePath="instance/ai"
        backHref={overrides.backHref ?? '/p/acme-ios/events'}
        isOwner={overrides.isOwner ?? true}
        // The first account of a self-hosted instance also operates it.
        isPlatformAdmin={overrides.isPlatformAdmin ?? true}
        publicDemo={overrides.publicDemo ?? false}
        projects={PROJECTS}
        onLeave={onLeave}
        onSignOut={onSignOut}
      />
    </>,
  )
  return { onLeave, onSignOut }
}

async function openPalette(): Promise<HTMLElement> {
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
  return await screen.findByRole('dialog')
}

describe('Settings command palette destinations', () => {
  it('offers every settings section the rail lists, at its own path', async () => {
    const { onLeave } = renderPalette()

    const palette = await openPalette()
    for (const group of visibleGroupsAll(true, true)) {
      for (const item of group.items) {
        expect(within(palette).getByText(item.label)).toBeInTheDocument()
      }
    }

    fireEvent.click(within(palette).getByText('Storage'))

    // The settings path travels with the href so the shell's unsaved-changes
    // predicate can tell "still in the instance group" from "leaving it".
    expect(onLeave).toHaveBeenCalledWith('/settings/instance/storage')
  })

  it('hides owner-only sections from a non-owner, exactly as the rail does', async () => {
    renderPalette({ isOwner: false, isPlatformAdmin: false })

    const palette = await openPalette()

    expect(within(palette).getByText('Profile')).toBeInTheDocument()
    expect(within(palette).queryByText('Limits')).toBeNull()
    expect(within(palette).queryByText('Runtime')).toBeNull()
    expect(within(palette).queryByText('Observability')).toBeNull()
  })

  it("offers an organization owner the organization's settings and none of the Platform's (F20 PR9)", async () => {
    renderPalette({ isOwner: true, isPlatformAdmin: false })

    const palette = await openPalette()

    expect(within(palette).getByText('Email')).toBeInTheDocument()
    expect(within(palette).getByText('AI')).toBeInTheDocument()
    expect(within(palette).getByText('Limits')).toBeInTheDocument()
    expect(within(palette).queryByText('Mail relay')).toBeNull()
    expect(within(palette).queryByText('AI & search')).toBeNull()
    expect(within(palette).queryByText('Storage')).toBeNull()
  })

  it('leaves out on a public demo the sections the demo refuses, exactly as the rail does', async () => {
    renderPalette({ publicDemo: true })

    const palette = await openPalette()

    for (const group of visibleGroupsAll(true, true, true)) {
      for (const item of group.items) {
        expect(within(palette).getByText(item.label)).toBeInTheDocument()
      }
    }
    for (const label of ['Email', 'AI', 'Semantic search', 'Photos', 'Trackers', 'Limits']) {
      expect(within(palette).queryByText(label), `public demo offered "${label}"`).toBeNull()
    }
  })

  it('goes to the project it names rather than to the first one in the list', async () => {
    const { onLeave } = renderPalette()

    const palette = await openPalette()
    fireEvent.click(within(palette).getByText('Acme iOS'))

    // The app palette bound itself to `projects[0]` on these routes, because no
    // /settings/* route carries a :slug — so it searched Acme Android and
    // navigated into it from a takeover bound to Acme iOS.
    expect(onLeave).toHaveBeenCalledWith('/p/acme-ios/overview')
  })

  it('offers nothing scoped to a project it cannot know it is in', async () => {
    renderPalette()

    const palette = await openPalette()
    fireEvent.change(within(palette).getByPlaceholderText(/Search settings/i), {
      target: { value: 'anomalies' },
    })

    // A settings route has no project in scope. Answering this query with some
    // project's Anomalies page — or its knowledge base — is a guess, and the
    // guess was always `projects[0]`.
    expect(within(palette).getByText('No matches.')).toBeInTheDocument()
  })

  it('routes Sign out through the shell so the leave-guard sees it', async () => {
    const { onSignOut } = renderPalette()

    const palette = await openPalette()
    fireEvent.click(within(palette).getByText('Sign out'))

    expect(onSignOut).toHaveBeenCalledTimes(1)
  })
})

describe('Settings command palette search (#238)', () => {
  it('finds a section by a keyword it does not show, one word at a time', async () => {
    renderPalette()

    const palette = await openPalette()
    fireEvent.change(within(palette).getByRole('combobox'), { target: { value: 'timezone' } })

    expect(within(palette).getByText('General')).toBeInTheDocument()
    expect(within(palette).queryByText('Members')).toBeNull()

    // Two words that only match together, across label and keywords.
    fireEvent.change(within(palette).getByRole('combobox'), { target: { value: 'api key' } })
    expect(within(palette).getByText('API keys')).toBeInTheDocument()
    expect(within(palette).queryByText('General')).toBeNull()
  })

  it('finds a section by the words the app palette finds it by', async () => {
    // Both palettes read the rail's keywords and nothing else: "version" used
    // to find System from the app and nothing in here.
    renderPalette()

    const palette = await openPalette()
    fireEvent.change(within(palette).getByRole('combobox'), { target: { value: 'version' } })
    expect(within(palette).getByText('System')).toBeInTheDocument()

    fireEvent.change(within(palette).getByRole('combobox'), { target: { value: 'embeddings' } })
    expect(within(palette).getByText('Semantic search')).toBeInTheDocument()
    expect(within(palette).getByText('AI & search')).toBeInTheDocument()

    // Platform storage holds event photos and keeps them: no retention there.
    fireEvent.change(within(palette).getByRole('combobox'), { target: { value: 'retention' } })
    expect(within(palette).queryByText('Storage')).toBeNull()
  })
})

describe('Settings command palette active row', () => {
  it('rings the keyboard-active row with the focus ring, as the app palette does', async () => {
    renderPalette()

    const palette = await openPalette()
    fireEvent.change(within(palette).getByRole('combobox'), { target: { value: 'storage' } })

    // cmdk keeps DOM focus in the input, so the row never matches
    // :focus-visible; the ring rides on aria-selected instead.
    const row = await waitFor(() => {
      const selected = within(palette)
        .getAllByRole('option')
        .find(option => option.getAttribute('aria-selected') === 'true')
      expect(selected).toBeDefined()
      return selected as HTMLElement
    })
    expect(row).toHaveClass('aria-selected:ring-2', 'aria-selected:ring-inset', 'aria-selected:ring-ring')
  })
})

describe('Settings command palette focus restore', () => {
  it('hands focus to the settings content instead of dropping it on <body>', async () => {
    renderPalette()

    // Nothing focused: the state a deep link or a reload leaves, where Radix's
    // own restore target is <body> and the next Tab restarts at the skip link,
    // ahead of the ~20-stop rail.
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)

    await openPalette()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

    await waitFor(() => {
      expect(document.activeElement).toBe(document.getElementById(SETTINGS_CONTENT_ID))
    })
  })

  it('hands focus back to whatever opened it', async () => {
    renderPalette()

    const opener = screen.getByTestId('opener')
    opener.focus()

    await openPalette()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

    await waitFor(() => {
      expect(document.activeElement).toBe(opener)
    })
  })
})
