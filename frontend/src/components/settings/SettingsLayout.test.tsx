import { useEffect, useState, type ReactNode } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { SettingsLayout } from './SettingsLayout'
import { WORKSPACE_GROUPS } from './nav'
import { useUnsavedChanges } from './unsaved-changes'
import type { Project } from '@/types'

// Mock the auth context so the layout renders as an owner and platform admin
// (the Platform group is the platform admin's) without pulling in the real provider/network.
vi.mock('@/components/auth-context', () => ({
  useAuth: () => ({
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      role: 'owner',
      // The first account of a self-hosted instance also operates it.
      is_platform_admin: true,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn(),
  }),
}))

// A DATA router, not MemoryRouter: SettingsLayout guards unsaved drafts with
// useBlocker, which needs one to exist at all (tripl-l33u.14).
function dataRouter(element: ReactNode, initialEntries: string[] = ['/settings/general']) {
  return createMemoryRouter([{ path: '*', element }], { initialEntries })
}

function renderSettings(activePath: string) {
  return render(
    <RouterProvider
      router={dataRouter(
        <SettingsLayout activePath={activePath} backHref="/">
          <div>content</div>
        </SettingsLayout>,
      )}
    />,
  )
}

describe('SettingsLayout signposting', () => {
  it('drops the subtitle that named only two of the four groups (ST-10)', () => {
    renderSettings('members')

    expect(screen.queryByText('Workspace & account configuration')).toBeNull()
  })

  it('names where the way back goes: the bound project, or the workspace (ST-4)', () => {
    const { unmount } = render(
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="members" backHref="/p/demo/events" projectName="Demo project">
            <div>content</div>
          </SettingsLayout>,
        )}
      />,
    )
    expect(screen.getByRole('link', { name: 'Back to Demo project' })).toHaveAttribute(
      'href',
      '/p/demo/events',
    )
    unmount()

    render(
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="members" backHref="/workspace">
            <div>content</div>
          </SettingsLayout>,
        )}
      />,
    )
    expect(screen.getByRole('link', { name: 'Back to workspace' })).toHaveAttribute('href', '/workspace')
    expect(screen.queryByRole('link', { name: /Back to project/i })).toBeNull()
  })

  it('names the current section in the phone header, with a way out (ST-11)', () => {
    renderSettings('instance/storage')

    const main = screen.getByRole('main')
    expect(within(main).getByText('Storage')).toBeInTheDocument()
    expect(within(main).getByRole('link', { name: 'Close settings' })).toHaveAttribute('href', '/')
  })

  it('does not repeat a group name as its sub-label (ST-7)', () => {
    renderSettings('members')

    // "Project Project" / "Organization Organization" while nothing names them.
    expect(screen.getAllByText('Project')).toHaveLength(1)
    expect(screen.getAllByText('Organization')).toHaveLength(1)
  })

  it('offers a labelled "Back to project" cross-link to the in-app surface', () => {
    render(
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="members" backHref="/p/demo/events">
            <div>content</div>
          </SettingsLayout>,
        )}
      />,
    )

    const back = screen.getByRole('link', { name: /Back to project/i })
    expect(back).toHaveAttribute('href', '/p/demo/events')
  })

  it('links the bound project\'s tracking plan and alerting from the Project group (#238 ST-5)', () => {
    render(
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="project/general" backHref="/p/demo/events" projectSlug="demo">
            <div>content</div>
          </SettingsLayout>,
        )}
      />,
    )

    expect(screen.getByRole('link', { name: 'Tracking plan & alerting' })).toHaveAttribute(
      'href',
      '/p/demo/event-types',
    )
  })

  it('tags the unbuilt Plan rules section "Soon" without renaming its link (ST-5 / PL-26)', () => {
    renderSettings('members')

    const planRules = screen.getByRole('link', { name: 'Plan rules' })
    expect(planRules).toHaveTextContent('Soon')
  })

  it('offers no tracking-plan link while no project is bound', () => {
    renderSettings('members')

    expect(screen.queryByRole('link', { name: 'Tracking plan & alerting' })).toBeNull()
  })

  it('shows a short descriptor for each visible nav group', () => {
    renderSettings('members')

    expect(screen.getByText('Shared across everyone in the organization')).toBeInTheDocument()
    expect(screen.getByText('Settings just for you')).toBeInTheDocument()
    // Who a group is for once, in the sub-label, not again in the description (ST-7).
    expect(screen.getByText('Server-wide settings')).toBeInTheDocument()
    expect(screen.getByText('Platform admin')).toBeInTheDocument()
  })
})

describe('SettingsLayout project switcher (ST-6)', () => {
  const PROJECTS = [
    { slug: 'demo', name: 'Demo' },
    { slug: 'other', name: 'Other' },
  ] as unknown as Project[]

  it('rebinds the Project sections to another project without leaving settings', async () => {
    const router = createMemoryRouter(
      [
        {
          path: '*',
          element: (
            <SettingsLayout
              activePath="project/plan-rules"
              backHref="/p/demo/events"
              projectName="Demo"
              projectSlug="demo"
              projects={PROJECTS}
            >
              <div>content</div>
            </SettingsLayout>
          ),
        },
      ],
      { initialEntries: ['/settings/project/plan-rules?project=demo'] },
    )
    render(<RouterProvider router={router} />)

    fireEvent.keyDown(screen.getByRole('button', { name: 'Switch project (current: Demo)' }), {
      key: 'Enter',
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Other' }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/settings/project/plan-rules')
    })
    expect(router.state.location.search).toBe('?project=other')
  })

  it('opens General for the picked project from a workspace section', async () => {
    const router = createMemoryRouter(
      [
        {
          path: '*',
          element: (
            <SettingsLayout activePath="members" backHref="/workspace" projects={PROJECTS}>
              <div>content</div>
            </SettingsLayout>
          ),
        },
      ],
      { initialEntries: ['/settings/members'] },
    )
    render(<RouterProvider router={router} />)

    fireEvent.keyDown(screen.getByRole('button', { name: 'Pick a project' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Demo' }))

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/settings/project/general')
    })
    expect(router.state.location.search).toBe('?project=demo')
  })
})

describe('SettingsLayout nav accessibility', () => {
  it('gives the icon-led AI nav links accessible names, the organization\'s and the platform\'s apart', () => {
    renderSettings('instance/ai')

    expect(screen.getByRole('link', { name: 'AI' })).toHaveAttribute('href', '/settings/organization/ai')
    expect(screen.getByRole('link', { name: 'AI & search' })).toHaveAttribute('href', '/settings/instance/ai')
  })

  it('every Platform settings nav entry is a real link with an accessible name', () => {
    renderSettings('instance/runtime')

    const instance = WORKSPACE_GROUPS.find((group) => group.label === 'Platform')
    expect(instance).toBeDefined()

    for (const item of instance!.items) {
      expect(item.label.trim().length).toBeGreaterThan(0)
      // As <button>s none of these could be cmd-clicked into a new tab,
      // middle-clicked, hovered for a URL or copied (tripl-wd66).
      expect(screen.getByRole('link', { name: item.label })).toHaveAttribute(
        'href',
        `/settings/${item.path}`,
      )
    }
  })

  it('marks the active rail entry as the current page', () => {
    renderSettings('instance/storage')

    expect(screen.getByRole('link', { name: 'Storage' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'AI' })).not.toHaveAttribute('aria-current')
  })
})

describe('SettingsLayout responsive rail', () => {
  /** The rail element — the only <aside> in this shell. */
  function rail(container: HTMLElement): HTMLElement {
    const found = container.querySelector('aside')
    expect(found).not.toBeNull()
    return found as HTMLElement
  }

  it('parks the rail off-canvas below md and pins it from md up', () => {
    const { container } = renderSettings('members')

    // Off-canvas by default (phones) but forced back into static flow at md, so
    // a 264px rail can never leave a 390px viewport a ~45px content column
    // (tripl-jfm3.40).
    expect(rail(container).className).toContain('-translate-x-full')
    expect(rail(container).className).toContain('md:static')
    expect(rail(container).className).toContain('md:translate-x-0')
  })

  it('opens and dismisses the rail from the phone-only hamburger', () => {
    const { container } = renderSettings('members')

    const open = screen.getByRole('button', { name: 'Open settings navigation' })
    expect(open).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(open)
    expect(rail(container).className).toContain('translate-x-0')
    expect(rail(container).className).not.toContain('-translate-x-full')

    fireEvent.click(screen.getByRole('button', { name: 'Close settings navigation' }))
    expect(rail(container).className).toContain('-translate-x-full')
  })

  it('closes the rail after picking a section so the content is visible', () => {
    const { container } = renderSettings('members')

    fireEvent.click(screen.getByRole('button', { name: 'Open settings navigation' }))
    fireEvent.click(screen.getByRole('link', { name: 'Profile' }))

    expect(rail(container).className).toContain('-translate-x-full')
  })
})

describe('SettingsLayout off-canvas rail keyboard behaviour (DS-11)', () => {
  function rail(container: HTMLElement): HTMLElement {
    return container.querySelector('aside') as HTMLElement
  }

  // test-setup's matchMedia answers "no match": a phone-width viewport.
  it('keeps a closed off-canvas rail out of the Tab order and the a11y tree', () => {
    const { container } = renderSettings('members')
    expect(rail(container)).toHaveAttribute('inert')
  })

  it('moves focus into the opened rail, closes it on Escape and returns focus', () => {
    const { container } = renderSettings('members')
    const open = screen.getByRole('button', { name: 'Open settings navigation' })
    open.focus()
    fireEvent.click(open)

    expect(rail(container)).not.toHaveAttribute('inert')
    expect(rail(container)).toContainElement(document.activeElement as HTMLElement)
    // The content behind the drawer is what goes inert while it is open.
    expect(container.querySelector('main')).toHaveAttribute('inert')

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(rail(container)).toHaveAttribute('inert')
    expect(container.querySelector('main')).not.toHaveAttribute('inert')
    expect(open).toHaveFocus()
  })

  it('lets Escape close only a dialog opened over the open rail, not the rail too', async () => {
    function DialogOverRail() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Open dialog
          </button>
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogContent>
              <DialogTitle>Layer on top</DialogTitle>
              <DialogDescription>Stands in for the settings palette.</DialogDescription>
            </DialogContent>
          </Dialog>
        </>
      )
    }
    const { container } = render(
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="members" backHref="/">
            <DialogOverRail />
          </SettingsLayout>,
        )}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Open settings navigation' }))
    expect(rail(container)).not.toHaveAttribute('inert')

    fireEvent.click(screen.getByRole('button', { name: 'Open dialog' }))
    const dialog = await screen.findByRole('dialog', { name: 'Layer on top' })
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement))

    // One press, dispatched where it really lands: the focused element in the
    // dialog. Radix handles it at document capture and marks it handled.
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Layer on top' })).toBeNull())
    expect(rail(container)).not.toHaveAttribute('inert')
  })

  it('never makes the pinned rail inert from md up', () => {
    const original = window.matchMedia
    window.matchMedia = ((query: string) => ({
      ...original(query),
      matches: query === '(min-width: 768px)',
    })) as typeof window.matchMedia
    try {
      const { container } = renderSettings('members')
      expect(rail(container)).not.toHaveAttribute('inert')
    } finally {
      window.matchMedia = original
    }
  })
})

describe('SettingsLayout unsaved-changes guard', () => {
  const DRAFT_MESSAGE = 'Instance settings you edited here have not been saved.'

  /** Stands in for ServiceSettingsPage: a draft only the instance group keeps. */
  function InstanceDraft({
    dirty = true,
    dirtyPaths,
  }: {
    dirty?: boolean
    dirtyPaths?: readonly string[]
  }) {
    const { registerUnsaved } = useUnsavedChanges()
    useEffect(() => {
      registerUnsaved(
        dirty
          ? { keptBy: (path) => path.startsWith('instance/'), message: DRAFT_MESSAGE, dirtyPaths }
          : null,
      )
      return () => registerUnsaved(null)
    }, [dirty, dirtyPaths, registerUnsaved])
    return <div>draft</div>
  }

  // Save on Instance is per section, so an edit left in another section needs
  // a pointer back to it on the rail (WS-23).
  it('marks the rail entries of sections with unsaved changes', async () => {
    const dirtyPaths = ['instance/security'] as const
    render(
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="instance/ai" backHref="/p/demo/events">
            <InstanceDraft dirtyPaths={dirtyPaths} />
          </SettingsLayout>,
          ['/settings/instance/ai'],
        )}
      />,
    )

    expect(
      await screen.findByRole('link', { name: 'Security & access, unsaved changes' }),
    ).toHaveAttribute('href', '/settings/instance/security')
    expect(screen.getByRole('link', { name: 'AI' })).toBeInTheDocument()
  })

  function draftShell(dirty: boolean) {
    return (
      <RouterProvider
        router={dataRouter(
          <SettingsLayout activePath="instance/ai" backHref="/p/demo/events">
            <InstanceDraft dirty={dirty} />
          </SettingsLayout>,
          ['/settings/instance/ai'],
        )}
      />
    )
  }

  function renderWithDraft() {
    return render(draftShell(true))
  }

  function reload(): Event {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event
  }

  /**
   * A router with real history behind the current entry, so `navigate(-1)` is
   * the browser Back button rather than a fresh navigation.
   */
  function backableRouter() {
    return createMemoryRouter(
      [
        {
          path: '*',
          element: (
            <SettingsLayout activePath="instance/ai" backHref="/p/demo/events">
              <InstanceDraft dirty />
            </SettingsLayout>
          ),
        },
      ],
      { initialEntries: ['/p/demo/events', '/settings/instance/ai'], initialIndex: 1 },
    )
  }

  // THE POINT OF tripl-l33u.14. Back was the one exit no guard could reach: a
  // plain BrowserRouter offers no blocker, and the history-parking workaround it
  // replaces could only react AFTER the browser had already moved. A blocker is
  // asked first, so the draft is still there to save when the dialog appears.
  it('warns before the browser Back button discards the draft', async () => {
    const router = backableRouter()
    render(<RouterProvider router={router} />)

    await act(async () => {
      await router.navigate(-1)
    })

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(
      'Leave without saving?',
    )
    // Not committed: the reader is still on the section, with the draft intact.
    expect(router.state.location.pathname).toBe('/settings/instance/ai')
  })

  it('stays put when the reader cancels a Back press', async () => {
    const router = backableRouter()
    render(<RouterProvider router={router} />)
    await act(async () => {
      await router.navigate(-1)
    })

    fireEvent.click(await screen.findByRole('button', { name: /keep editing/i }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(router.state.location.pathname).toBe('/settings/instance/ai')
  })

  it('goes back once the reader accepts the loss', async () => {
    const router = backableRouter()
    render(<RouterProvider router={router} />)
    await act(async () => {
      await router.navigate(-1)
    })

    fireEvent.click(await screen.findByRole('button', { name: /discard changes/i }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/p/demo/events'))
  })

  it('warns before a rail link navigates the draft out of existence', async () => {
    renderWithDraft()

    fireEvent.click(screen.getByRole('link', { name: 'Profile' }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(
      'Leave without saving?',
    )
    expect(screen.getByRole('alertdialog')).toHaveTextContent(DRAFT_MESSAGE)
  })

  it('warns before "Back to project" leaves the settings area', async () => {
    renderWithDraft()

    fireEvent.click(screen.getByRole('link', { name: /Back to project/i }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(
      'Leave without saving?',
    )
  })

  it('warns before the command palette navigates the draft out of existence', async () => {
    renderWithDraft()

    // Ctrl+K is live on every settings route, so it is a way out of the
    // takeover like any other and has to meet the same guard.
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    const palette = await screen.findByRole('dialog')
    fireEvent.click(within(palette).getByText('Profile'))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(
      'Leave without saving?',
    )
    expect(screen.getByRole('alertdialog')).toHaveTextContent(DRAFT_MESSAGE)
  })

  it('stays silent when a palette destination keeps the draft', async () => {
    renderWithDraft()

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    const palette = await screen.findByRole('dialog')
    fireEvent.click(within(palette).getByText('Mail relay'))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('warns before Sign out drops the draft', async () => {
    renderWithDraft()

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('alertdialog')).toHaveTextContent(DRAFT_MESSAGE)
  })

  it('stays silent when the destination keeps the draft', () => {
    // AI → Mail relay keeps ServiceSettingsPage mounted, so a prompt here would
    // be a false alarm on every section switch (tripl-l8v2).
    renderWithDraft()

    fireEvent.click(screen.getByRole('link', { name: 'Mail relay' }))

    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('lets the browser prompt before a reload or tab close discards the draft', () => {
    // Neither is a React navigation, so the dialog above can never run for
    // them — only a beforeunload listener reaches them (tripl-l33u.6).
    renderWithDraft()

    expect(reload().defaultPrevented).toBe(true)
  })

  it('leaves reload alone while there is nothing to lose', () => {
    renderSettings('instance/ai')

    expect(reload().defaultPrevented).toBe(false)
  })

  it('stops prompting on reload once the draft is saved', () => {
    const { rerender } = render(draftShell(true))

    rerender(draftShell(false))

    expect(reload().defaultPrevented).toBe(false)
  })
})

describe('SettingsLayout landmarks and headings', () => {
  it('offers a skip link to a focusable main landmark', () => {
    const { container } = renderSettings('members')

    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip).toHaveAttribute('href', '#settings-content')

    const main = container.querySelector('#settings-content')
    expect(main).not.toBeNull()
    expect(main).toHaveAttribute('tabindex', '-1')
  })

  it('names the rail nav without emitting an h2 above the page h1', () => {
    renderSettings('members')

    // The rail used to render <h2>Settings</h2> before every page's <h1>,
    // which opened the heading outline with a level-2 skip (tripl-jfm3.69).
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()
    expect(screen.getByRole('navigation', { name: 'Settings' })).toBeInTheDocument()
  })
})

describe('SettingsLayout content width', () => {
  it('gives a table section the wide column and keeps forms narrow', () => {
    const { container, unmount } = renderSettings('platform/orgs')
    expect(container.querySelector('[data-width]')).toHaveAttribute('data-width', 'wide')
    unmount()

    const narrow = renderSettings('profile')
    expect(narrow.container.querySelector('[data-width]')).toHaveAttribute('data-width', 'narrow')
  })
})
