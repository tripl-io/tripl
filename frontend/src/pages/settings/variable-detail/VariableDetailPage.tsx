import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ShieldCheck } from 'lucide-react'
import { variableOverridesApi } from '@/api/variableOverrides'
import { variablesApi } from '@/api/variables'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { Panel } from '@/components/settings/kit'
import { ImpactNotice } from '@/components/dependencies/ImpactNotice'
import { UsedBySection } from '@/components/dependencies/UsedBySection'
import { usePageTitle } from '@/components/shell-chrome-context'
import { EntityNotFound, PageSkeleton, ReadOnlyNotice } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { variableOverridesKey, variablesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { Variable } from '@/types'
import { bindingExample } from '../bindingExample'
import { TYPE_LABELS } from '../variablesShared'
import { useVariableDefinitionDraft } from './useVariableDefinitionDraft'
import { VariableDefinitionFields } from './VariableDefinitionFields'
import { VariableDriftSection } from './VariableDriftSection'
import { VariableObservedSection } from './VariableObservedSection'
import { VariableOverridesSection } from './VariableOverridesSection'
import {
  isVariableDetailTab,
  VARIABLE_DETAIL_TABS,
  variableDetailPath,
  variableListPath,
  type VariableDetailTab,
} from './variableDetailPath'

/**
 * One variable's own page, `/p/:slug/variables/:id` (AU-26).
 *
 * Everything about a variable used to live in one `max-w-4xl` edit dialog: the
 * definition form, value-drift triage, per-event overrides with their own
 * picker and Save, and the observed-values table — with a Save and Cancel that
 * applied only to the top part, and no address to share. Here each is a tab of
 * its own, the definition keeps its Save in view, and the sections that act at
 * once sit apart from it. The list's quick-edit dialog is built from the same
 * sections.
 *
 * Reads the variable off the project's variables list (the cache the events
 * pages already share); there is no single-variable endpoint to ask.
 */
export function VariableDetailPage({ slug, variableId }: { slug: string; variableId: string }) {
  const navigate = useNavigate()
  const branchId = useActiveBranchId()
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const tab: VariableDetailTab = isVariableDetailTab(tabParam) ? tabParam : 'definition'
  // Replaced rather than pushed: switching tabs is not a navigation worth a
  // Back step. The definition draft lives on this component, so it survives a
  // tab switch and needs no guard for one.
  const setTab = (next: VariableDetailTab) =>
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev)
        if (next === 'definition') params.delete('tab')
        else params.set('tab', next)
        return params
      },
      { replace: true },
    )

  const { data, isSuccess, isError, error, refetch } = useQuery({
    queryKey: variablesKey(slug, branchId),
    queryFn: () => variablesApi.list(slug, branchId),
    meta: SILENT_ERROR_META,
  })
  const variables = useMemo(() => data ?? [], [data])
  const variable = variables.find((v) => v.id === variableId)
  const example = useMemo(() => bindingExample(variables), [variables])
  usePageTitle(variable ? `\${${variable.name}}` : undefined)

  // A branch deep-copies variables under new ids, so the id in the URL belongs
  // to ONE branch. Switching branch here follows the variable by name, the
  // identity that survives the copy — the same rule as the event-type page
  // (PLAN-44). Remembered during render, like any value followed from a prop.
  const [lastSeenName, setLastSeenName] = useState<string | null>(null)
  if (variable && variable.name !== lastSeenName) setLastSeenName(variable.name)
  const sameNameOnThisBranch =
    !variable && isSuccess && lastSeenName !== null
      ? variables.find((v) => v.name === lastSeenName)
      : undefined
  const redirectTo = sameNameOnThisBranch
    ? `${variableDetailPath(slug, sameNameOnThisBranch.id)}${searchParams.toString() ? `?${searchParams.toString()}` : ''}`
    : null
  useEffect(() => {
    if (redirectTo) navigate(redirectTo, { replace: true })
  }, [navigate, redirectTo])

  const back = (
    <Link
      to={variableListPath(slug, variable?.id)}
      className="inline-flex items-center gap-1 text-caption text-fg-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-3" aria-hidden="true" />
      Variables
    </Link>
  )

  if (isError && data === undefined) {
    return (
      <PageContainer className="space-y-4">
        {back}
        <ErrorState
          compact
          title="Couldn't load this variable"
          error={error}
          onRetry={() => { void refetch() }}
          retryLabel="Retry"
        />
      </PageContainer>
    )
  }

  if (isSuccess && !variable && !sameNameOnThisBranch) {
    return (
      <EntityNotFound
        title="Variable not found"
        description={
          branchId === null
            ? 'This variable does not exist on main. It may have been deleted, renamed or retired by a scan.'
            : 'This variable does not exist on the selected branch. It may have been deleted, renamed, or only exist on another branch.'
        }
        back={{ to: variableListPath(slug), label: 'Back to variables' }}
      />
    )
  }

  if (!variable) return redirectTo ? null : <PageSkeleton variant="detail" label="Loading variable…" />

  // Keyed by id: another variable (or the same one on another branch) starts
  // on a fresh draft.
  return (
    <VariableDetailBody
      key={variable.id}
      slug={slug}
      branchId={branchId}
      variable={variable}
      example={example}
      tab={tab}
      onTabChange={setTab}
      back={back}
      refreshError={isError ? error : null}
    />
  )
}

function VariableDetailBody({
  slug,
  branchId,
  variable,
  example,
  tab,
  onTabChange,
  back,
  refreshError,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  example: ReturnType<typeof bindingExample>
  tab: VariableDetailTab
  onTabChange: (next: VariableDetailTab) => void
  back: ReactNode
  refreshError: unknown
}) {
  const canWrite = useCanWriteProject()
  const draft = useVariableDefinitionDraft({ slug, branchId, variable, canWrite })
  // Leaving the page with an unsaved definition asks first.
  const unsaved = useUnsavedChangesGuard(canWrite && draft.dirty)
  const { data: overrides } = useQuery({
    queryKey: variableOverridesKey(slug, branchId, variable.id),
    queryFn: () => variableOverridesApi.list(slug, variable.id, branchId),
  })

  const counts: Partial<Record<VariableDetailTab, number | undefined>> = {
    drift: variable.open_drift_count,
    overrides: overrides?.length,
    observed: variable.context_count,
  }
  const saveStatus = draft.updateMut.isPending
    ? 'Saving…'
    : !draft.dirty
      ? (draft.updateMut.isSuccess ? 'Saved' : 'No changes')
      : draft.typeChangeBlocked
        ? 'Fix the values the new type refuses'
        : 'Unsaved changes'

  return (
    <PageContainer className="space-y-3.5">
      {unsaved.dialog}
      <PageHeader
        back={back}
        eyebrow="Plan · Variable"
        title={<span className="mono">{`\${${variable.name}}`}</span>}
        titleAddon={
          <>
            <Chip variant="outline" size="xs">{TYPE_LABELS[variable.variable_type]}</Chip>
            {variable.excluded_from_scans ? (
              <Chip tone="neutral" size="xs" title="Scans skip this variable.">Excluded from scans</Chip>
            ) : null}
          </>
        }
        description={variable.description || undefined}
      />
      {refreshError ? (
        <p role="alert" className="text-body-sm text-destructive">
          Couldn't refresh this variable: {getErrorMessage(refreshError)}
        </p>
      ) : null}

      <Tabs
        value={tab}
        onValueChange={(next) => onTabChange(next as VariableDetailTab)}
        className="gap-[18px]"
      >
        <TabsList aria-label="Variable sections">
          {VARIABLE_DETAIL_TABS.map((t) => (
            <TabsTrigger
              key={t.id}
              value={t.id}
              count={counts[t.id] || undefined}
            >
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="definition" className="max-w-[880px] space-y-3">
          {!canWrite && <ReadOnlyNotice />}
          <form noValidate onSubmit={draft.handleSubmit}>
            <Panel
              title="Definition"
              subtitle="What the variable stands for, the values it may take, and where scans find it."
            >
              <div className="p-4">
                <VariableDefinitionFields
                  slug={slug}
                  branchId={branchId}
                  variable={variable}
                  draft={draft}
                  example={example}
                  canWrite={canWrite}
                />
              </div>
            </Panel>
            {/* A rename re-points `${old}` in stored values, but not in SQL or
                anything outside the plan: name what reads it (#257). Warning
                only — Save stays as it was. */}
            {canWrite && draft.name.trim() !== '' && draft.name !== variable.name && (
              <ImpactNotice
                slug={slug}
                branchId={branchId}
                changes={[{ kind: 'variable', id: variable.id, change: 'rename' }]}
              />
            )}
            {canWrite && (
              // Sticky to the bottom of the viewport while the form runs past
              // it, so Save stays in reach below a long value list.
              <div className="sticky bottom-0 z-10 mt-3 flex items-center justify-end gap-3 rounded-card border bg-surface px-4 py-2.5">
                <span className="text-caption text-fg-muted" aria-live="polite">{saveStatus}</span>
                <Button
                  type="submit"
                  size="sm"
                  disabled={!draft.dirty || draft.updateMut.isPending || draft.typeChangeBlocked}
                >
                  Save changes
                </Button>
              </div>
            )}
          </form>
          <UsedBySection slug={slug} entity={{ kind: 'variable', id: variable.id }} branchId={branchId} />
        </TabsContent>

        <TabsContent value="drift" className="max-w-[880px]">
          <VariableDriftSection
            slug={slug}
            branchId={branchId}
            variable={variable}
            canWrite={canWrite}
            empty={
              <EmptyState
                size="sm"
                icon={ShieldCheck}
                title="No value drift"
                description="Scans have seen no values outside the documented list. When they do, each one waits here for a verdict."
              />
            }
          />
        </TabsContent>

        <TabsContent value="overrides" className="max-w-[880px]">
          <VariableOverridesSection
            slug={slug}
            branchId={branchId}
            variable={variable}
            variableType={variable.variable_type}
            canWrite={canWrite}
            note="Save override applies it at once."
          />
        </TabsContent>

        <TabsContent value="observed">
          <VariableObservedSection
            slug={slug}
            branchId={branchId}
            variable={variable}
            canWrite={canWrite}
            scrollClassName=""
          />
        </TabsContent>
      </Tabs>
    </PageContainer>
  )
}
