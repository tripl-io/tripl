import { useId } from 'react'
import { Link, useInRouterContext, useNavigate } from 'react-router-dom'
import { ArrowUpRight } from 'lucide-react'
import type { Variable } from '@/types'
import { Button } from '@/components/ui/button'
import { ImpactNotice } from '@/components/dependencies/ImpactNotice'
import { Dialog, DialogBody, DialogClose, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useDialogLeave } from '@/components/ui/dialog-guard'
import type { BindingExample } from './bindingExample'
import { useVariableDefinitionDraft } from './variable-detail/useVariableDefinitionDraft'
import { VariableDefinitionFields } from './variable-detail/VariableDefinitionFields'
import { VariableDriftSection } from './variable-detail/VariableDriftSection'
import { VariableObservedSection } from './variable-detail/VariableObservedSection'
import { VariableOverridesSection } from './variable-detail/VariableOverridesSection'
import { variableDetailPath } from './variable-detail/variableDetailPath'

/**
 * The quick editor for ONE variable, opened from the list: its definition,
 * with drift review, per-event overrides and observed values under it.
 *
 * The same sections make up the variable's own page
 * (`/p/:slug/variables/:id`), which the header links to: the
 * page gives each section room and a shareable address; this dialog is for a
 * quick fix without leaving the list.
 *
 * Its own component so its form state, queries and sub-panels re-render on a
 * keystroke without the whole variables page behind it. The page
 * mounts it keyed by the variable id, so every variable opens on a fresh form.
 *
 * `variable` is the LIVE row from the page's list, not a copy taken when the
 * dialog opened: after "Clear observed values" or a drift action the list
 * refetches, and a snapshot kept offering to clear "12 contexts" of a variable
 * that had none left. Only the form drafts are seeded once.
 */
export function VariablesEditDialog({
  slug,
  branchId,
  variable,
  canWrite,
  example,
  onClose,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  canWrite: boolean
  example: BindingExample
  onClose: () => void
}) {
  const formId = useId()
  const inRouter = useInRouterContext()
  const draft = useVariableDefinitionDraft({ slug, branchId, variable, canWrite, onSaved: onClose })

  return (
    // The same draft its own page guards: a stray click outside, Escape or
    // Cancel asks before dropping an edited definition.
    <Dialog open dirty={canWrite && draft.dirty} onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent className="max-w-4xl">
        {/* pr-8 keeps a long name from running under the close button. */}
        <DialogHeader className="pr-8">
          <DialogTitle className="break-all leading-tight">{canWrite ? 'Edit' : 'Property'}: {variable.name}</DialogTitle>
        </DialogHeader>
        {/* Only the body scrolls: the title and Save stay in view.
            min-w-0 all the way down: the observed-values table's min-content
            width used to widen the body past a 390px dialog and clip every
            control at the right edge. */}
        <DialogBody className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4">
          {/* The form holds the definition only. The sections below act at
              once, and outside it an Enter in one of their inputs cannot
              submit the definition. Save in the footer reaches
              the form through `form=`. */}
          <form id={formId} noValidate className="min-w-0" onSubmit={draft.handleSubmit}>
            <VariableDefinitionFields
              slug={slug}
              branchId={branchId}
              variable={variable}
              draft={draft}
              example={example}
              canWrite={canWrite}
            />
            {/* What reads the variable, once its name is edited (#257). */}
            {canWrite && draft.name.trim() !== '' && draft.name !== variable.name && (
              <ImpactNotice
                slug={slug}
                branchId={branchId}
                changes={[{ kind: 'variable', id: variable.id, change: 'rename' }]}
              />
            )}
          </form>
          <VariableDriftSection slug={slug} branchId={branchId} variable={variable} canWrite={canWrite} />
          <VariableOverridesSection
            slug={slug}
            branchId={branchId}
            variable={variable}
            variableType={draft.type}
            canWrite={canWrite}
            note="Save override applies it at once; the dialog's Save and Cancel do not touch it."
          />
          <VariableObservedSection slug={slug} branchId={branchId} variable={variable} canWrite={canWrite} />
        </DialogBody>
        <DialogFooter>
          {/* In the footer, not the header: the dialog's first focus belongs
              to the Name field, not to a way out of the dialog. */}
          {inRouter && <OpenPropertyPageLink to={variableDetailPath(slug, variable.id)} />}
          <DialogClose asChild>
            <Button type="button" variant="outline">{canWrite ? 'Cancel' : 'Close'}</Button>
          </DialogClose>
          {canWrite && (
            <Button type="submit" form={formId} disabled={draft.updateMut.isPending || draft.typeChangeBlocked || draft.schemaIssues.length > 0}>
              Save
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The way from the quick editor to the property's own page. A link, so it
 * opens in a new tab as well; a plain click with an edited definition asks
 * first, as Cancel does, instead of leaving the draft behind.
 */
function OpenPropertyPageLink({ to }: { to: string }) {
  const navigate = useNavigate()
  const leave = useDialogLeave()
  return (
    <Link
      to={to}
      onClick={event => {
        // A modified click opens another tab and leaves this dialog as it is.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return
        event.preventDefault()
        leave(() => navigate(to))
      }}
      className="inline-flex items-center gap-0.5 self-center text-caption font-medium text-accent hover:underline sm:mr-auto"
    >
      Open property page
      <ArrowUpRight className="size-3" aria-hidden="true" />
    </Link>
  )
}
