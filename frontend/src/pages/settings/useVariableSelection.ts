import { useCallback, useState } from 'react'

/**
 * The variables table's bulk selection, and the parts of its invariant that do
 * not need the page's controls.
 *
 * The invariant: the selection only ever names rows the operator can see in the
 * current match set. Four things move that set, and each is held where it
 * happens:
 *
 *  - a control that redraws the match set wholesale (filter text, usage filter)
 *    clears the selection — `clear`, called by the page's `changeMatchSet`;
 *  - a row-level action that moves ONE row out (Exclude, Delete) drops that id
 *    — `deselect`;
 *  - a branch switch clears it — here, beside the state;
 *  - data moving under a selection nobody touched is pruned — here, given the
 *    ids that still match.
 *
 * See VariablesTab for why each of those exists.
 */
export function useVariableSelection({
  branchId,
  matchingIds,
  loaded,
}: {
  branchId: string | null
  /** Ids of every variable the current filters match, across all pages. */
  matchingIds: ReadonlySet<string>
  /** False until the list has answered, so a first load never reads as "nothing matches". */
  loaded: boolean
}) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  // BranchSwitcher is mounted permanently in the app sidebar, so it is reachable
  // whenever a selection exists — and switching re-keys the variables query and
  // repaints a completely different row set while `selectedIds` sits untouched.
  // The bar then read "12 selected" for twelve ids that are not on the branch
  // now on screen, and every bulk action carried them: `_load_variables_by_ids`
  // filters by branch and 404s the WHOLE call on the first id it cannot find, so
  // a bulk edit aimed at the rows in front of the operator failed wholesale.
  //
  // The reset lives beside the state it guards because the sidebar switcher has
  // no way to call a helper in the page. Adjusting during render with an
  // equality guard is how this repo follows a prop change (see
  // ProjectAlertingTab.tsx); an effect would let one frame of the new branch
  // paint under the old count.
  const [selectionBranchId, setSelectionBranchId] = useState(branchId)
  if (selectionBranchId !== branchId) {
    setSelectionBranchId(branchId)
    setSelectedIds(new Set())
  }

  // The net under the invariant, for match-set changes NO CONTROL ANNOUNCES. A
  // bulk "Add values" or "Set description" is the case that bites: usage is
  // answered SERVER-side by the retirement predicate, which keeps a row for its
  // documented values or for an edit someone made, so the update makes its own
  // rows stop being "unused" and the list comes back without them. The bar was
  // then left floating "12 selected", with a Delete button, over the "Nothing to
  // retire" empty state. A colleague's delete and a retiring scan land here too.
  //
  // This is not the intersection `changeMatchSet` rejects: a filter change clears
  // the selection before it can ever reach this line. Pruning during render
  // converges in one extra render; an effect would leave a window in which
  // Delete could post ids the page had already decided to forget.
  if (loaded && selectedIds.size > 0) {
    const stillMatching = [...selectedIds].filter((id) => matchingIds.has(id))
    if (stillMatching.length !== selectedIds.size) {
      setSelectedIds(new Set(stillMatching))
    }
  }

  const toggle = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  /** Drops ONE id, for a row-level action that has just moved that row out of
   * the match set. Returns `prev` untouched when the id was not selected, so the
   * common case (acting on a row while nothing is ticked) does not re-render the
   * table. */
  const deselect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [])

  const clear = useCallback(() => setSelectedIds(new Set()), [])
  const selectAll = useCallback((ids: Iterable<string>) => setSelectedIds(new Set(ids)), [])

  return { selectedIds, toggle, deselect, clear, selectAll }
}
