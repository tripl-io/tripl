/**
 * Where to go after signing in: the page RequireAuth bounced the visitor from,
 * with its query string and fragment intact.
 *
 * Only the pathname used to survive, and the query is what anchors the links
 * people actually follow while signed out: an alert message's
 * `?item=…&incident=…`, a branch diff's `?branch=…`, `?section=monitors`.
 */
export function postLoginDestination(state: unknown): string {
  const from = (state as { from?: { pathname?: string; search?: string; hash?: string } } | null)
    ?.from
  if (!from?.pathname) return '/'
  return `${from.pathname}${from.search ?? ''}${from.hash ?? ''}`
}
