import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronDown, GitBranch, GitCompare, Plus, Settings2 } from 'lucide-react'
import { toast } from 'sonner'
import { planBranchesApi } from '@/api/planBranches'
import { useBranchContext } from '@/hooks/useBranch'
import { requestPageLeave } from '@/hooks/useUnsavedChangesGuard'
import { Chip } from '@/components/primitives/chip'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { PlanBranchSummary } from '@/types'
import { planBranchesKey } from '@/lib/queryKeys'
import { STATUS_LABEL, STATUS_TONE } from '@/lib/branchStatus'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export function BranchSwitcher({ slug, compact = false }: { slug: string; compact?: boolean }) {
  const { branchId, setBranchId } = useBranchContext()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)

  const branchesQuery = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug),
    enabled: Boolean(slug),
  })

  const branches = useMemo(() => branchesQuery.data?.items ?? [], [branchesQuery.data])
  const mainBranch = useMemo(() => branches.find((b) => b.kind === 'main'), [branches])
  const workingBranches = useMemo(
    () => branches.filter((b) => b.kind !== 'main' && b.status !== 'merged' && b.status !== 'closed'),
    [branches],
  )

  const active = branchId ? (branches.find((b) => b.id === branchId) ?? null) : (mainBranch ?? null)
  // Until the list arrives a selected branch has no name to show, and "main"
  // would be a claim about data the page is not reading.
  const resolving = !!branchId && branchesQuery.isPending
  const activeLabel = resolving ? 'loading…' : (active?.name ?? 'main')
  const onMain = !branchId || active?.kind === 'main'

  // Switching branch swaps the data under the page without a navigation, so a
  // form with a draft would be remounted empty. Ask the page's unsaved-changes
  // guard first; the popover closes either way.
  // The switch is said out loud: it changes where every edit goes, and the
  // rail is not where the eye is.
  const switchTo = (id: string | null) => {
    setOpen(false)
    if (id === branchId) return
    const target = id ? branches.find((b) => b.id === id) : null
    requestPageLeave(() => {
      setBranchId(id)
      toast(
        target
          ? `Switched to ${target.name} — edits stay on this branch until it merges`
          : 'Switched to main — edits now change the live plan',
        { id: 'branch-switched' },
      )
    })
  }

  // "New branch" opens the create dialog on the branches page (`?new=1`)
  // rather than only landing on the list, where it had to be found again.
  // Managing the list is its own item.
  const goToBranches = (create: boolean) => {
    setOpen(false)
    navigate(projectPath(currentOrgSlug(), slug, `/branches${create ? '?new=1' : ''}`))
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {compact ? (
          // The collapsed rail's form: an icon with the branch in its name and
          // a dot when the pages read a feature branch rather than main.
          <button
            type="button"
            title={`Branch: ${activeLabel}`}
            aria-label={`Switch branch (current: ${activeLabel})`}
            className="relative flex h-8 w-8 items-center justify-center rounded-md transition-colors hover:bg-sidebar-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg-secondary"
          >
            <GitBranch className="size-4" aria-hidden="true" />
            {!onMain && (
              <span
                aria-hidden="true"
                className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-info"
              />
            )}
          </button>
        ) : (
        // On a branch the trigger wears the info edge the shell's branch strip
        // uses, the full name in its title (the label truncates), and the
        // branch's status instead of a "feature" chip that said nothing.
        <button
          type="button"
          title={`Switch branch (current: ${activeLabel})`}
          className="flex h-7 w-full items-center gap-1.5 rounded-control border px-2 text-caption transition-colors hover:bg-sidebar-hover"
          style={{
            background: 'transparent',
            borderColor: 'var(--border-subtle)',
            ...(onMain ? null : { borderLeft: '2px solid var(--info)' }),
          }}
        >
          <GitBranch
            className="size-3 shrink-0"
            style={{ color: onMain ? 'var(--accent)' : 'var(--info)' }}
            aria-hidden="true"
          />
          <span className="mono min-w-0 flex-1 truncate text-left text-fg">
            {activeLabel}
          </span>
          {!onMain && active ? (
            <Chip tone={STATUS_TONE[active.status]} size="xs" className="shrink-0">
              {STATUS_LABEL[active.status]}
            </Chip>
          ) : null}
          <ChevronDown className="size-3 shrink-0 text-fg-tertiary" aria-hidden="true" />
        </button>
        )}
      </PopoverTrigger>
      <PopoverContent align="start" side={compact ? 'right' : 'bottom'} className="w-[260px] p-1.5">
        <div
          className="px-2 pb-1.5 pt-1 micro-label text-fg-tertiary"
        >
          Plan branches
          {branchesQuery.isFetching && (
            <span className="ml-1.5 normal-case tracking-normal text-fg-tertiary">
              loading…
            </span>
          )}
        </div>
        <div className="max-h-[300px] overflow-y-auto">
          {mainBranch && (
            <BranchRow
              branch={mainBranch}
              active={onMain}
              onSelect={() => switchTo(null)}
            />
          )}
          {workingBranches.map((branch) => (
            <BranchRow
              key={branch.id}
              branch={branch}
              active={branchId === branch.id}
              onSelect={() => switchTo(branch.id)}
            />
          ))}
          {!branchesQuery.isFetching && workingBranches.length === 0 && (
            <div className="px-2 py-1.5 text-caption text-fg-tertiary">
              No active branches yet.
            </div>
          )}
        </div>
        <div
          className="mt-1 border-t pt-1 border-border-subtle"
        >
          <button
            type="button"
            onClick={() => goToBranches(true)}
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-body-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
          >
            <Plus className="size-3 shrink-0" aria-hidden="true" />
            New branch from main
          </button>
          <button
            type="button"
            onClick={() => goToBranches(false)}
            className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-body-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
          >
            <Settings2 className="size-3 shrink-0" aria-hidden="true" />
            Manage branches
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

function BranchRow({
  branch,
  active,
  onSelect,
}: {
  branch: PlanBranchSummary
  active: boolean
  onSelect: () => void
}) {
  const isMain = branch.kind === 'main'
  const Icon = isMain ? GitBranch : GitCompare
  return (
    <button
      type="button"
      onClick={onSelect}
      title={branch.name}
      aria-current={active ? 'true' : undefined}
      className="flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-body-sm transition-colors hover:bg-[var(--surface-hover)]"
      style={{ color: active ? 'var(--fg)' : 'var(--fg-muted)' }}
    >
      <Icon
        className="size-3 shrink-0"
        style={{ color: isMain ? 'var(--accent)' : 'var(--fg-subtle)' }}
        aria-hidden="true"
      />
      <span className="mono min-w-0 flex-1 truncate">{branch.name}</span>
      {/* Which branch waits for review and which is approved, so the right
          one can be picked from here. */}
      {!isMain && (
        <Chip tone={STATUS_TONE[branch.status]} size="xs" className="shrink-0">
          {STATUS_LABEL[branch.status]}
        </Chip>
      )}
      {active && <Check className="size-3 shrink-0 text-accent" aria-hidden="true" />}
    </button>
  )
}
