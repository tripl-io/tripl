import type { ReactNode } from 'react'
import { RotateCcw, Trash2 } from 'lucide-react'
import type { VariableRetirementCounts } from '@/api/projects'
import { Button } from '@/components/ui/button'
import { NativeSelect } from '@/components/settings/kit'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { RESET_PERIODS } from './projectGeneralFields'

/** The danger-zone rows of Project settings › General. */

/**
 * A settings row with text on the left and an action group on the right, which
 * stacks until the ROW is 560px wide. Side by side at 375px, a period Select
 * (up to 280px) plus a button left the text a few characters wide and pushed
 * the row past the card (WS-14). Keyed to the viewport's `sm`, the same thing
 * happened at 768px, where the pinned settings rail leaves the card ~430px and
 * the hint became a one-word-per-line column (ST-1). So it reads the width of
 * its container, like the kit's FormRow: put it inside a
 * {@link DANGER_ROW_CONTAINER_CLASS} element.
 */
export const DANGER_ROW_CLASS =
  'flex flex-col gap-3 px-4 py-[14px] @min-[560px]:flex-row @min-[560px]:items-center @min-[560px]:gap-[18px]'

/** The query container a {@link DANGER_ROW_CLASS} row measures. */
export const DANGER_ROW_CONTAINER_CLASS = '@container'

export function DangerRow({
  title,
  hint,
  action,
  last,
}: {
  title: string
  hint: string
  action: ReactNode
  last?: boolean
}) {
  return (
    <div
      className={DANGER_ROW_CONTAINER_CLASS}
      style={{ borderBottom: last ? 'none' : '1px solid var(--border-subtle)' }}
    >
      <div className={DANGER_ROW_CLASS}>
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium">{title}</div>
          <div className="mt-[3px] text-body-sm leading-[1.45] text-fg-tertiary">
            {hint}
          </div>
        </div>
        {action}
      </div>
    </div>
  )
}

/**
 * A danger-zone row that clears a category of detections over a chosen period.
 * The period selector sits next to a destructive button; confirmation and the
 * mutation are owned by the caller. Feedback (counts / error) renders under it.
 */
export function DangerResetRow({
  title,
  hint,
  buttonLabel,
  period,
  onPeriodChange,
  onReset,
  busy,
  feedback,
}: {
  title: string
  hint: string
  buttonLabel: string
  period: string
  onPeriodChange: (value: string) => void
  onReset: () => void
  busy: boolean
  feedback: ReactNode
}) {
  return (
    <div className={`${DANGER_ROW_CONTAINER_CLASS} border-b border-b-border-subtle`}>
      <div className={DANGER_ROW_CLASS}>
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium">{title}</div>
          <div className="mt-[3px] text-body-sm leading-[1.45] text-fg-tertiary">
            {hint}
          </div>
          {feedback}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <NativeSelect
            aria-label={`${title} period`}
            value={period}
            onChange={onPeriodChange}
            options={RESET_PERIODS}
            disabled={busy}
          />
          {/* Bare red in a row; the solid red is the confirm's (DS-20). */}
          <Button variant="danger" size="sm" disabled={busy} onClick={onReset}>
            <RotateCcw className="h-3 w-3" />
            {busy ? 'Resetting…' : buttonLabel}
          </Button>
        </div>
      </div>
    </div>
  )
}

/**
 * The retirement row is two buttons, not one: a preview that commits nothing,
 * and a destructive apply that only lights up once the preview has said how
 * many rows it would take. Scans mint variables and, before this shipped, never
 * retired one, so a project can arrive here carrying four figures of them — the
 * count IS the decision, and asking for it separately is what makes the second
 * click informed rather than brave.
 */
export function DangerRetireVariablesRow({
  onPreview,
  onRetire,
  busy,
  preview,
  feedback,
  last,
}: {
  onPreview: () => void
  onRetire: () => void
  busy: boolean
  preview: VariableRetirementCounts | undefined
  feedback: ReactNode
  last?: boolean
}) {
  // Why Retire is grey, said beside it: a disabled button alone read as a
  // neutral chip with no hint of the Preview it waits for (ST-38).
  const retireBlocker = busy
    ? null
    : !preview
      ? 'Run Preview first.'
      : preview.retirable === 0
        ? 'Nothing to retire.'
        : null
  return (
    <div
      className={DANGER_ROW_CONTAINER_CLASS}
      style={{ borderBottom: last ? 'none' : '1px solid var(--border-subtle)' }}
    >
      <div className={DANGER_ROW_CLASS}>
        <div className="min-w-0 flex-1">
          <div className="text-body font-medium">Retire unused properties</div>
          <div className="mt-[3px] text-body-sm leading-[1.45] text-fg-tertiary">
            Delete properties a scan created that no event field value references and that carry no
            observed values, drift or documented values. Nothing edited by hand is touched.
          </div>
          {feedback}
        </div>
        <div className="flex flex-col items-start gap-1 @min-[560px]:items-end">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" disabled={busy} onClick={onPreview}>
              {busy ? 'Checking…' : 'Preview'}
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={busy || !preview || preview.retirable === 0}
              onClick={onRetire}
              {...disabledReasonAria('retire-variables', retireBlocker)}
            >
              <Trash2 className="h-3 w-3" />
              Retire
            </Button>
          </div>
          <DisabledReason id="retire-variables" reason={retireBlocker} tone="muted" />
        </div>
      </div>
    </div>
  )
}
