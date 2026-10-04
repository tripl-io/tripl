import { useEffect, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * Keyboard shortcuts for the shell: `?` opens the shortcut sheet, `c`
 * presses the current page's create button, and `g` then a letter goes to a
 * project page. Ctrl/⌘ K (the palette) and `/` (a list's search box) live with
 * their own components.
 */

/** How long after `g` the second key still counts as part of the sequence. */
export const GO_TO_TIMEOUT_MS = 1000

/**
 * `g` then a letter: the project pages a hand reaches for most, by the first
 * letter of their sidebar name where it is free. Paths are relative to
 * `/p/:slug`, and only offered inside a project.
 */
export const GO_TO_SHORTCUTS: readonly { key: string; path: string; label: string }[] = [
  { key: 'o', path: 'overview', label: 'Overview' },
  { key: 'e', path: 'events', label: 'Events' },
  { key: 't', path: 'event-types', label: 'Event types' },
  { key: 'm', path: 'metrics', label: 'Metrics' },
  { key: 'n', path: 'anomalies', label: 'Anomalies' },
  { key: 'a', path: 'alerting', label: 'Alerting' },
  { key: 's', path: 'scans', label: 'Scans' },
  { key: 'b', path: 'branches', label: 'Plan branches' },
]

/** Opt-in marker for a page's create control when its label is not "New …". */
export const CREATE_ACTION_ATTR = 'data-create-action'

/** Any text field, where a letter is typing, not a command. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  if (target.closest('[role="textbox"], [role="combobox"], .cm-editor')) return true
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

/** A dialog, menu or listbox is up: the key belongs to it, not the page. */
function layerOpen(): boolean {
  return (
    document.querySelector(
      '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]',
    ) !== null
  )
}

function usable(element: HTMLElement): boolean {
  if (element.closest('[hidden], [inert], [aria-hidden="true"]')) return false
  if (element instanceof HTMLButtonElement && element.disabled) return false
  return element.getAttribute('aria-disabled') !== 'true'
}

/**
 * The page's create control: one marked with `data-create-action`, else the
 * first button or link in the page whose label starts with "New" — the
 * app's wording for every create CTA ("New event", "New metric").
 */
export function findCreateAction(root: ParentNode | null): HTMLElement | null {
  if (!root) return null
  const marked = root.querySelector<HTMLElement>(`[${CREATE_ACTION_ATTR}]`)
  if (marked && usable(marked)) return marked
  const candidates = root.querySelectorAll<HTMLElement>('button, a[href]')
  for (const candidate of candidates) {
    const label = (candidate.getAttribute('aria-label') ?? candidate.textContent ?? '').trim()
    if (/^New\b/.test(label) && usable(candidate)) return candidate
  }
  return null
}

export function useShellShortcuts({ onOpenHelp }: { onOpenHelp: () => void }): void {
  const navigate = useNavigate()
  const { slug } = useParams()
  // When `g` was pressed, or null outside a sequence. A ref, not state: it
  // only matters to the next keydown and must not re-render the shell.
  const goPressedAtRef = useRef<number | null>(null)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return
      if (event.repeat || isTypingTarget(event.target) || layerOpen()) {
        goPressedAtRef.current = null
        return
      }
      // A sequence ends on the very next key, matched or not; a late second
      // key is read on its own.
      const goPressedAt = goPressedAtRef.current
      goPressedAtRef.current = null
      if (goPressedAt !== null && Date.now() - goPressedAt <= GO_TO_TIMEOUT_MS && slug) {
        const target = GO_TO_SHORTCUTS.find((shortcut) => shortcut.key === event.key)
        if (target) {
          event.preventDefault()
          void navigate(projectPath(currentOrgSlug(), slug, `/${target.path}`))
          return
        }
      }
      if (event.key === 'g' && !event.shiftKey && slug) {
        goPressedAtRef.current = Date.now()
        return
      }
      if (event.key === '?') {
        event.preventDefault()
        onOpenHelp()
        return
      }
      if (event.key === 'c' && !event.shiftKey) {
        const action = findCreateAction(document.getElementById(MAIN_CONTENT_ID))
        if (!action) return
        event.preventDefault()
        action.click()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onOpenHelp, navigate, slug])
}
