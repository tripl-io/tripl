import { useCallback, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import {
  applyViewParams,
  deleteEventsSavedView,
  loadEventsSavedViews,
  saveEventsSavedView,
  viewParamsOf,
  type EventsSavedView,
} from './savedViews'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

type ConfirmFn = (options: {
  title: string
  message: string
  variant?: 'danger' | 'primary'
  confirmLabel?: string
}) => Promise<boolean>

/**
 * Holds the URL-derived "saved views" state for the events page: persisted
 * list, currently-named view, save/apply/delete handlers. The host page only
 * needs to pass slug + activeTab and render the resulting handlers.
 */
export function useSavedViews({
  slug,
  activeTab,
  confirm,
}: {
  slug: string | undefined
  activeTab: string
  /** Asks before a save replaces a view of the same name, and before a delete. */
  confirm: ConfirmFn
}) {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  const [savedViews, setSavedViews] = useState<EventsSavedView[]>(
    () => (slug ? loadEventsSavedViews(slug) : []),
  )
  const [storedSlug, setStoredSlug] = useState(slug)
  // Reset via setState during render (React-recommended) instead of effect — the
  // initial-load case is handled by lazy useState(), this handles slug changes.
  if (storedSlug !== slug) {
    setStoredSlug(slug)
    setSavedViews(slug ? loadEventsSavedViews(slug) : [])
  }

  const [savedViewName, setSavedViewName] = useState('')

  const currentSavedViewParams = viewParamsOf(searchParams)

  // Compared as normalized pairs: views saved before normalization still match
  // when their keys come back in another order.
  const activeSavedViewName = useMemo(
    () => savedViews.find(view => (
      view.tab === activeTab && viewParamsOf(view.params) === currentSavedViewParams
    ))?.name ?? null,
    [activeTab, currentSavedViewParams, savedViews],
  )

  const saveCurrentView = useCallback(async () => {
    if (!slug) return
    const name = savedViewName.trim()
    if (!name) return
    // Saving under a taken name used to replace that view without a word.
    if (savedViews.some(view => view.name === name)) {
      const ok = await confirm({
        title: 'Replace saved view',
        message: `A view named "${name}" already exists. Replace it with the current filters?`,
        confirmLabel: 'Replace',
      })
      if (!ok) return
    }
    const nextViews = saveEventsSavedView(slug, {
      name,
      tab: activeTab,
      params: currentSavedViewParams,
    })
    setSavedViews(nextViews)
    setSavedViewName('')
  }, [activeTab, confirm, currentSavedViewParams, savedViewName, savedViews, slug])

  const applySavedView = useCallback((view: EventsSavedView) => {
    if (!slug) return
    const path = view.tab === 'all' ? projectPath(currentOrgSlug(), slug, '/events') : projectPath(currentOrgSlug(), slug, `/events/${view.tab}`)
    const params = applyViewParams(searchParams, view.params).toString()
    navigate(path + (params ? `?${params}` : ''), { replace: true })
  }, [navigate, searchParams, slug])

  // A view is a hand-built filter set with no undo, and the trash icon sat
  // right beside the row that applies it — it went on one click.
  const deleteSavedView = useCallback(async (name: string) => {
    if (!slug) return
    const ok = await confirm({
      title: 'Delete saved view',
      message: `Delete the saved view "${name}"? Its filters are not kept anywhere else.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (!ok) return
    setSavedViews(deleteEventsSavedView(slug, name))
  }, [confirm, slug])

  return {
    savedViews,
    savedViewName,
    setSavedViewName,
    activeSavedViewName,
    saveCurrentView,
    applySavedView,
    deleteSavedView,
  }
}
