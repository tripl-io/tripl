import { createContext, useContext, useEffect } from 'react'

/**
 * What a routed page may ask of the app shell around it. Kept in its own module
 * so pages do not import Layout.
 */
export type ShellChromeContextValue = {
  /** Hide the activity rail while the calling page is mounted. */
  suppressActivityRail: (suppressed: boolean) => void
  /** Name the entity the page shows in the top bar; null hands it back. */
  setPageTitle: (title: string | null) => void
}

export const ShellChromeContext = createContext<ShellChromeContextValue>({
  suppressActivityRail: () => {},
  setPageTitle: () => {},
})

/**
 * Where the shell forwards the entity a detail page names, so the browser-tab
 * title (driven from the app root, above the shell) can use it too.
 */
export const DocumentEntityTitleContext = createContext<(title: string | null) => void>(() => {})

/**
 * Keep the activity rail out of the way while this page is shown — a 404 has
 * one job, the way back, and a 20-item feed beside it pulled the eye away
 * from it.
 */
export function useSuppressActivityRail(): void {
  const { suppressActivityRail } = useContext(ShellChromeContext)
  useEffect(() => {
    suppressActivityRail(true)
    return () => suppressActivityRail(false)
  }, [suppressActivityRail])
}

/**
 * What an editor names itself with: "Edit · Active Sessions". The browser tab
 * reads it whole; on the editor routes that know the prefix, the top bar splits
 * it into "Metrics › Active Sessions › Edit" (#246).
 */
export const EDIT_PAGE_TITLE_PREFIX = 'Edit · '

export function editPageTitle(name: string): string {
  return `${EDIT_PAGE_TITLE_PREFIX}${name}`
}

/**
 * Put the entity a detail page shows in the top bar instead of the route's
 * generic "Detail". The breadcrumb is hidden below `sm`, so on a
 * phone that word was all the bar said about where the user was. Pass nothing
 * until the entity has loaded; the route's own title stands until then and
 * comes back when the page unmounts.
 */
export function usePageTitle(title: string | null | undefined): void {
  const { setPageTitle } = useContext(ShellChromeContext)
  useEffect(() => {
    if (!title) return
    setPageTitle(title)
    return () => setPageTitle(null)
  }, [title, setPageTitle])
}
