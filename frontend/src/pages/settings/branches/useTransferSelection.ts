import { useCallback, useMemo, useState } from 'react'

import type { PlanBranchDiffSummary, PlanDiffEntry } from '@/types'
import { entryRowKey } from './branchDiffModel'
import {
  selectionWithRenames,
  transferableEntry,
  transferRenamePairs,
} from './branchTransferModel'

/**
 * The rows ticked for "Move/Copy to branch…", by `entryRowKey`.
 *
 * Held by key rather than by entry so a refetched diff keeps the ticks on
 * rows that are still there and drops the ones that are gone.
 */
export function useTransferSelection(diff: PlanBranchDiffSummary | undefined) {
  const [keys, setKeys] = useState<ReadonlySet<string>>(new Set())
  const present = useMemo(
    () => new Set((diff?.entries ?? []).map((entry) => entryRowKey(entry))),
    [diff],
  )
  const live = useMemo(
    () => new Set([...keys].filter((key) => present.has(key))),
    [keys, present],
  )
  const toggle = useCallback((entry: PlanDiffEntry) => {
    if (!transferableEntry(entry)) return
    const key = entryRowKey(entry)
    setKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])
  const clear = useCallback(() => setKeys(new Set()), [])
  // What the request sends: the ticked rows plus their rename halves.
  const entries = useMemo(() => selectionWithRenames(diff, live), [diff, live])
  const renamePairs = useMemo(() => transferRenamePairs(diff), [diff])
  return {
    isSelected: (entry: PlanDiffEntry) => live.has(entryRowKey(entry)),
    toggle,
    clear,
    /** Rows the user ticked, as the list counts them. */
    count: live.size,
    entries,
    /** The diff's renames as halves, so a rename counts as one change. */
    renamePairs,
  }
}
