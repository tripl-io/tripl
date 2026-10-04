import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { eventsApi } from '@/api/events'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventIdentityProbeKey } from '@/lib/queryKeys'

/** An event already holding the identity the form is about to claim. */
export interface IdentityHolder {
  id: string
  name: string
}

/** An event this form created, with the type it was created under: identities
 *  are unique per event type (`_event_holding_scan_identity` keys on both), so
 *  a name taken under one type is still free under another. */
export interface CreatedIdentity extends IdentityHolder {
  eventTypeId: string
}

/**
 * Advisory duplicate check. The SERVER is what refuses a taken scan identity
 * (409 from create_event); this only spares the user filling a whole form to
 * find out on submit. It asks the exact-name lookup (`GET /events/by-names`),
 * which answers with the same rule create refuses on. The substring
 * `search` it used before could miss the row when a broad match pushed it past
 * the page limit.
 *
 * `createdHere` is what this form has itself created. "Save and add another"
 * keeps the values, so they regenerate the name just taken, and the probe's
 * cached "not taken" answer outlived the save — Save stayed enabled and the
 * second press met the server's 409 instead of this warning. The form
 * also invalidates the probes on a create; this closes the window until the
 * refetch lands.
 */
export function useEventIdentityProbe({
  slug,
  branchId,
  eventTypeId,
  completedName,
  enabled,
  createdHere,
}: {
  slug: string
  branchId: string | null
  eventTypeId: string
  /** The composed name, or null while fields it is built from are missing. */
  completedName: string | null
  enabled: boolean
  createdHere: readonly CreatedIdentity[]
}): IdentityHolder | null {
  const probedName = useDebouncedValue(completedName, 350)
  const { data: identityProbe } = useQuery({
    queryKey: eventIdentityProbeKey(slug, branchId, eventTypeId, probedName),
    queryFn: ({ signal }) => eventsApi.byNames(slug, eventTypeId, [probedName!], branchId, signal),
    enabled: enabled && !!probedName && !!eventTypeId,
    // Advisory: a failed check shows no warning, and the server still refuses.
    meta: SILENT_ERROR_META,
  })
  return useMemo(() => {
    if (!enabled || !completedName) return null
    const mine = createdHere.find(
      item => item.name === completedName && item.eventTypeId === eventTypeId,
    )
    if (mine) return mine
    if (probedName !== completedName || !identityProbe) return null
    // The server applied the two arms of `_event_holding_scan_identity`: an
    // event answering to this identity, or one with no identity yet whose name
    // the next scan will adopt as one.
    const holder = identityProbe.items.find(item => item.identity === probedName)
    return holder ? { id: holder.event_id, name: holder.name } : null
  }, [enabled, completedName, createdHere, eventTypeId, probedName, identityProbe])
}
