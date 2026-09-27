import { useId, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import { projectTemplatesApi } from '@/api/projectTemplates'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectTemplatesKey } from '@/lib/queryKeys'
import type { ProjectTemplateSummary } from '@/types/projectTemplates'
import {
  BLANK_PROJECT_LABEL,
  NEEDS_LABEL,
  SUGGESTIONS_NOTE,
  formatTemplateMeta,
} from './projectTemplateCopy'

type Option = { id: string | null; template: ProjectTemplateSummary | null }

/**
 * "Start from" choice in the New project dialog (F21, #274): a blank project
 * (the default) or one of the industry templates. A radio group with one Tab
 * stop on the checked card and arrow keys moving the choice, like RadioCards
 * in the settings kit. Below the group, a disclosure lists the checked
 * template's starter metrics and alert rules, which are suggestions only:
 * nothing creates them until a data source or alert destination exists.
 *
 * Templates are optional sugar, so a list that fails to load (or comes back
 * empty) leaves Blank project as the one choice rather than blocking create.
 */
export function ProjectTemplatePicker({
  value,
  onChange,
  disabled,
  describedBy,
}: {
  /** The chosen template id; null is Blank project. */
  value: string | null
  onChange: (templateId: string | null) => void
  disabled?: boolean
  /** Id of help text under the picker that the group should announce. */
  describedBy?: string
}) {
  const baseId = useId()
  const legendId = `${baseId}-legend`
  const statusId = `${baseId}-status`
  const radioRefs = useRef<(HTMLButtonElement | null)[]>([])

  const templatesQuery = useQuery({
    queryKey: projectTemplatesKey(),
    queryFn: ({ signal }) => projectTemplatesApi.list(signal),
    // The picker says so itself and falls back to Blank project; a toast
    // on top would make an optional list sound like a failed create.
    meta: SILENT_ERROR_META,
    staleTime: 5 * 60_000,
  })

  const templates = templatesQuery.data ?? []
  const options: Option[] = [
    { id: null, template: null },
    ...templates.map((template) => ({ id: template.id, template })),
  ]
  const checkedIndex = options.findIndex((option) => option.id === value)
  const tabStop = checkedIndex >= 0 ? checkedIndex : 0
  const checkedTemplate = options[tabStop]?.template ?? null

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number | null = null
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      next = (index + 1) % options.length
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      next = (index - 1 + options.length) % options.length
    } else if (event.key === 'Home') {
      next = 0
    } else if (event.key === 'End') {
      next = options.length - 1
    }
    if (next === null || disabled) return
    event.preventDefault()
    const option = options[next]
    if (!option) return
    onChange(option.id)
    radioRefs.current[next]?.focus()
  }

  let status: string | null = null
  if (templatesQuery.isPending) status = 'Loading templates…'
  else if (templatesQuery.isError)
    status = 'Templates could not be loaded. You can still start with a blank project.'
  else if (templates.length === 0) status = 'No templates are available.'

  const groupDescribedBy = [status ? statusId : null, describedBy].filter(Boolean).join(' ')

  return (
    <div className="grid gap-2">
      <p id={legendId} className="m-0 text-body font-medium leading-none">
        Start from
      </p>
      <div
        role="radiogroup"
        aria-labelledby={legendId}
        aria-describedby={groupDescribedBy || undefined}
        aria-busy={templatesQuery.isPending || undefined}
        className="grid gap-2"
      >
        {options.map((option, index) => {
          const checked = index === tabStop
          const key = option.id ?? '__blank__'
          const nameId = `${baseId}-${key}-name`
          const detailId = `${baseId}-${key}-detail`
          const template = option.template
          return (
            <button
              key={key}
              ref={(element) => {
                radioRefs.current[index] = element
              }}
              type="button"
              role="radio"
              aria-checked={checked}
              aria-labelledby={nameId}
              aria-describedby={detailId}
              tabIndex={index === tabStop ? 0 : -1}
              disabled={disabled}
              onClick={() => onChange(option.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
              className="flex w-full items-start gap-2.5 rounded-card px-[13px] py-[11px] text-left disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                border: `1px solid ${checked ? 'var(--accent)' : 'var(--border)'}`,
                background: checked ? 'var(--accent-soft)' : 'var(--bg)',
              }}
            >
              <span
                aria-hidden="true"
                className="mt-px flex size-4 shrink-0 items-center justify-center rounded-full"
                style={{ border: `1.5px solid ${checked ? 'var(--accent)' : 'var(--input)'}` }}
              >
                {checked && <span className="size-2 rounded-full bg-accent" />}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span id={nameId} className="text-body-sm font-semibold text-fg">
                  {template ? template.name : BLANK_PROJECT_LABEL}
                </span>
                <span id={detailId} className="flex flex-col gap-0.5">
                  <span className="text-caption leading-[1.4] text-fg-tertiary">
                    {template
                      ? template.description
                      : 'Start with an empty plan and add your own events.'}
                  </span>
                  {/* Kept apart in the accessible description too; in the
                      flex column the space itself is not rendered. */}
                  {template && ' '}
                  {template && (
                    <span className="text-caption leading-[1.4] text-fg-secondary">
                      {formatTemplateMeta(template)}
                    </span>
                  )}
                </span>
              </span>
            </button>
          )
        })}
      </div>
      {/* Outside the radiogroup, which may own only radios: one disclosure for
          the checked template, remounted per template so it starts closed. */}
      {checkedTemplate && (
        <TemplateSuggestions key={checkedTemplate.id} template={checkedTemplate} />
      )}
      {status && (
        <p
          id={statusId}
          role="status"
          className="m-0 text-caption text-fg-tertiary"
        >
          {status}
        </p>
      )}
    </div>
  )
}

function TemplateSuggestions({ template }: { template: ProjectTemplateSummary }) {
  const [open, setOpen] = useState(false)
  const noteId = useId()
  const { metric_suggestions: metrics, alert_suggestions: alerts } = template
  if (metrics.length === 0 && alerts.length === 0) return null
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="px-[13px]">
      <CollapsibleTrigger
        className="inline-flex items-center gap-1 text-caption font-medium text-accent hover:underline"
        aria-describedby={noteId}
      >
        <ChevronRight
          aria-hidden="true"
          className={`size-3 transition-transform${open ? ' rotate-90' : ''}`}
        />
        <span aria-hidden="true">Starter metrics and alerts</span>
        <span className="sr-only">{`Starter metrics and alerts for ${template.name}`}</span>
      </CollapsibleTrigger>
      <p id={noteId} className="m-0 mt-1 text-caption leading-[1.4] text-fg-tertiary">
        {SUGGESTIONS_NOTE}
      </p>
      <CollapsibleContent className="mt-1.5 grid gap-2 text-caption leading-[1.4]">
        {metrics.length > 0 && (
          <div>
            <p className="m-0 font-semibold text-fg-secondary">Starter metrics</p>
            <ul className="m-0 list-disc pl-4 text-fg-secondary">
              {metrics.map((metric) => (
                <li key={metric.name}>
                  <span className="font-medium text-fg">{metric.display_name}</span>
                  {metric.description ? ` — ${metric.description}` : ''}{' '}
                  <span className="text-fg-tertiary">({NEEDS_LABEL[metric.needs]})</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {alerts.length > 0 && (
          <div>
            <p className="m-0 font-semibold text-fg-secondary">Starter alert rules</p>
            <ul className="m-0 list-disc pl-4 text-fg-secondary">
              {alerts.map((alert) => (
                <li key={alert.name}>
                  <span className="font-medium text-fg">{alert.name}</span>
                  {alert.description ? ` — ${alert.description}` : ''}{' '}
                  <span className="text-fg-tertiary">(needs an alert destination)</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
