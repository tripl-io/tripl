import { useCallback, useEffect, useLayoutEffect, useMemo, useState, type ReactNode } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { planBranchesApi } from '@/api/planBranches'
import { planBranchesKey } from '@/lib/queryKeys'
import { requestPageLeave } from '@/hooks/useUnsavedChangesGuard'
import type { PlanBranchSummary } from '@/types'
import { BranchContext, type SetBranchOptions } from './branch-context-internal'
import { orgStorageKey } from '@/lib/activeOrg'

const STORAGE_PREFIX = 'tripl-branch:'
const BRANCH_PARAM = 'branch'
const BRANCHES_REFRESH_MS = 60_000

function readStored(slug: string | null): string | null {
  if (!slug || typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`))
    return raw && raw.length > 0 ? raw : null
  } catch {
    return null
  }
}

function writeStored(slug: string, value: string | null) {
  if (typeof window === 'undefined') return
  try {
    if (value) {
      window.localStorage.setItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`), value)
    } else {
      window.localStorage.removeItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`))
    }
  } catch {
    /* ignore */
  }
}

/** Why a selected branch can no longer be worked in, or null while it can. */
function staleReason(
  branchId: string,
  branches: readonly PlanBranchSummary[],
): { name: string | null; reason: 'merged' | 'closed' | 'missing' } | null {
  const branch = branches.find((b) => b.id === branchId)
  if (!branch) return { name: null, reason: 'missing' }
  if (branch.kind === 'main') return null
  if (branch.status === 'merged' || branch.status === 'closed') {
    return { name: branch.name, reason: branch.status }
  }
  return null
}

function staleMessage(name: string | null, reason: 'merged' | 'closed' | 'missing'): string {
  if (reason === 'missing') return 'The branch you were working in no longer exists; switched to main.'
  return `Branch ${name ?? ''} was ${reason}; switched to main.`
}

export function BranchProvider({ slug, children }: { slug: string | null; children: ReactNode }) {
  return (
    <BranchProviderState key={slug ?? '__root__'} slug={slug}>
      {children}
    </BranchProviderState>
  )
}

function BranchProviderState({ slug, children }: { slug: string | null; children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const { pathname } = useLocation()
  const urlBranch = searchParams.get(BRANCH_PARAM)
  // The URL is the source of truth for a feature branch: a shared link carries
  // ?branch=<id> (branch-diff rows link this way) and wins over the visitor's
  // stored selection. Storage only fills in when the URL names none, so a
  // plain in-app link keeps the branch the user chose.
  const [branchId, setBranchIdState] = useState<string | null>(() => urlBranch ?? readStored(slug))
  // The branch known to have been a live working branch while selected: the
  // one read back from storage (it was selected before), or one the list has
  // reported live since. Only that one is dropped when it ends; a merged branch
  // somebody deliberately opens by link stays on screen as a read-only view.
  const [workedIn, setWorkedIn] = useState<string | null>(() => (urlBranch ? null : readStored(slug)))

  // Follow the URL when it changes: Back / Forward across entries with a
  // different ?branch=, or a link that carries one. An entry WITHOUT the param
  // leaves the selection alone. Render-time adjustment, not an effect, so no
  // frame renders the page on the previous branch.
  const [lastUrl, setLastUrl] = useState({ branch: urlBranch, pathname })
  // A ?branch= change on the SAME path (Back into an older entry of this page)
  // is not seen by the page's navigation blocker, yet it swaps the data under
  // the page like the branch switcher does, so it asks the same question.
  const [pendingUrlBranch, setPendingUrlBranch] = useState<{ target: string; kept: string | null } | null>(null)
  if (lastUrl.branch !== urlBranch || lastUrl.pathname !== pathname) {
    setLastUrl({ branch: urlBranch, pathname })
    if (lastUrl.branch !== urlBranch && urlBranch && urlBranch !== branchId) {
      // A different path has already been through the router's blocker.
      if (lastUrl.pathname !== pathname) setBranchIdState(urlBranch)
      else setPendingUrlBranch({ target: urlBranch, kept: branchId })
    }
  }

  useLayoutEffect(() => {
    if (!pendingUrlBranch) return
    const { target, kept } = pendingUrlBranch
    requestPageLeave(
      () => {
        setPendingUrlBranch(null)
        setBranchIdState(target)
      },
      () => {
        setPendingUrlBranch(null)
        // Keep the draft's branch, and make the address say so again.
        setSearchParams(
          (prev) => {
            const params = new URLSearchParams(prev)
            if (kept) params.set(BRANCH_PARAM, kept)
            else params.delete(BRANCH_PARAM)
            return params
          },
          { replace: true },
        )
      },
    )
    // setSearchParams changes identity on every navigation; this runs once per
    // pending adoption.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingUrlBranch])

  const setBranchId = useCallback(
    (next: string | null, options?: SetBranchOptions) => {
      setBranchIdState(next)
      if (options?.updateUrl === false || searchParams.get(BRANCH_PARAM) === next) return
      // Switching rewrites the current address, so a reload, a copied URL or a
      // Back into this entry all agree with what the user picked.
      const params = new URLSearchParams(searchParams)
      if (next) params.set(BRANCH_PARAM, next)
      else params.delete(BRANCH_PARAM)
      setSearchParams(params, { replace: true })
    },
    [searchParams, setSearchParams],
  )

  // A merged, closed or deleted branch must not stay selected: every
  // branch-scoped request would keep naming it, reading a frozen plan or 404ing
  // under a switcher label that says main. Checked here rather than in the
  // switcher so pages that do not render the switcher are covered too. The list
  // is refreshed every minute and on coming back to the tab after half that, so
  // a merge in another session is noticed without a reload.
  const branchesQuery = useQuery({
    queryKey: planBranchesKey(slug ?? undefined),
    queryFn: () => planBranchesApi.list(slug!),
    enabled: !!slug && !!branchId,
    staleTime: BRANCHES_REFRESH_MS / 2,
    refetchOnWindowFocus: true,
    refetchInterval: BRANCHES_REFRESH_MS,
  })
  const found = branchId && branchesQuery.isSuccess ? staleReason(branchId, branchesQuery.data.items) : null
  if (branchId && branchesQuery.isSuccess && !found && workedIn !== branchId) setWorkedIn(branchId)
  // "Missing" only from an answer that is not being replaced: a branch created
  // a moment ago is absent from the list cached before it existed.
  const stale =
    found && (found.reason === 'missing' ? !branchesQuery.isFetching : workedIn === branchId) ? found : null
  const [dropped, setDropped] = useState<{ id: string; message: string } | null>(null)
  if (branchId && stale) {
    setBranchIdState(null)
    setDropped({ id: branchId, message: staleMessage(stale.name, stale.reason) })
  }

  useEffect(() => {
    if (!dropped) return
    toast.info(dropped.message, { id: `branch-dropped:${dropped.id}` })
    // Strip the dead id from the address too, or a reload brings it back.
    setSearchParams(
      (prev) => {
        if (prev.get(BRANCH_PARAM) !== dropped.id) return prev
        const params = new URLSearchParams(prev)
        params.delete(BRANCH_PARAM)
        return params
      },
      { replace: true },
    )
    // setSearchParams changes identity on every navigation; this runs once per
    // dropped branch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dropped])

  // Persistence has one home: whatever the branch ends up as — switched by hand,
  // adopted from a ?branch= link, or read back from storage — is written here.
  useEffect(() => {
    if (slug) writeStored(slug, branchId)
  }, [slug, branchId])

  const value = useMemo(() => ({ branchId, setBranchId, slug }), [branchId, setBranchId, slug])
  return <BranchContext.Provider value={value}>{children}</BranchContext.Provider>
}
