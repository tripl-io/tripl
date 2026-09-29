/**
 * The per-field pieces of the single-event form: the control a field or meta
 * field renders, and the notes under it. Split out of `EventForm.tsx`, which
 * had grown to 1,600 lines holding five helpers, the route page and the form
 * itself (EVT-30).
 */
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { Check } from 'lucide-react'
import type { FieldDefinition, MetaFieldDefinition, Variable } from '@/types'
import { ChipListInput } from '@/components/chip-list-input'
import { useCopyToClipboard } from '@/hooks/useCopyToClipboard'
import {
  META_FIELD_LINK_EXAMPLE_KEY,
  metaFieldLinkExample,
  stripLinkTemplate,
} from '@/lib/metaFields'
import { JsonEditor } from './JsonEditor'
import { VariableInput } from './VariableInput'
import type { VariableSuggestion } from './variableSuggestions'
import { resolveTemplateTokens } from './utils'
import { SelectControl } from './eventFormLayout'
import { FieldError } from '@/components/forms/FieldError'
import { CodeToken } from '@/components/primitives/code-token'
import { isNumberFieldValue } from './eventFormValues'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * Says which way a field value is heading. The two things an analyst reaches
 * for have opposite and permanent effects, and neither announced itself:
 * typing a correction sets `is_authored`, after which `_upsert_field_values`
 * skips the field for good, while clearing the box deletes the row and the
 * next scan fills it in again.
 */
export function ScanMaintenanceNotice({
  stored,
  current,
  onHandBack,
}: {
  /** The saved row behind this box, or null on a field the event never carried. */
  stored: { value: string; isAuthored: boolean } | null
  current: string
  /**
   * Absent where clearing the box would 422 on save — a required field, or one
   * the scan's name format builds the event's identity out of.
   */
  onHandBack?: () => void
}) {
  if (!stored) return null
  if (stored.isAuthored) {
    if (current.trim() === '') {
      return (
        <p className="mt-1 text-body-sm text-fg-tertiary">
          Cleared. Save, and the next scan fills this in again.
        </p>
      )
    }
    return (
      <p className="mt-1 text-body-sm text-fg-tertiary">
        Edited by hand, so scans leave it alone.{' '}
        {onHandBack && (
          <button
            type="button"
            onClick={onHandBack}
            className="underline underline-offset-2 hover:text-foreground"
          >
            Hand back to scans
          </button>
        )}
      </p>
    )
  }
  if (current === stored.value) return null
  return (
    <p className="mt-1 text-body-sm text-warning">Saving this stops scans from updating the field.</p>
  )
}

/**
 * Points a field at the Breakdowns tab, which already answers "and what else
 * does this field hold?" — one value per box is all an event can carry, so the
 * question came up every time a scanned value looked wrong. The tab needed the
 * column in the breakdown set and a reader who knew the tab existed; this is
 * both, from where the question is asked.
 *
 * It does not toggle the column itself (AU-23): a second switch far from the
 * Metric breakdowns row changed the set without the reader seeing why. It says
 * whether the field is split, and takes the reader to the row's toggle.
 */
export function FieldBreakdownLink({
  column,
  href,
  state,
  onShowBreakdowns,
}: {
  column: string
  href: { to: string; onClick: () => void }
  /** `collecting` = added in this session and not yet saved, so there is nothing to open. */
  state: 'collected' | 'collecting' | 'off'
  /** Brings the column's toggle in the Metric breakdowns row into view. */
  onShowBreakdowns: () => void
}) {
  if (state === 'collected') {
    return (
      <p className="mt-1 text-body-sm">
        <Link
          to={href.to}
          onClick={href.onClick}
          className="text-fg-tertiary underline underline-offset-2 hover:text-foreground"
        >
          See every value this field takes
        </Link>
      </p>
    )
  }
  if (state === 'collecting') {
    return (
      <p className="mt-1 flex flex-wrap items-center gap-x-1 text-body-sm text-fg-tertiary">
        <button
          type="button"
          onClick={onShowBreakdowns}
          className="inline-flex items-center gap-1 text-success underline underline-offset-2 hover:text-foreground"
        >
          <Check className="size-3.5" aria-hidden="true" />
          Split by this field
        </button>
        <span>
          · Save, and collection starts splitting by <span className="mono">{column}</span>.
        </span>
      </p>
    )
  }
  return (
    <p className="mt-1 text-body-sm">
      <button
        type="button"
        onClick={onShowBreakdowns}
        className="text-fg-tertiary underline underline-offset-2 hover:text-foreground"
      >
        Split volume by this field
      </button>
    </p>
  )
}

export function FieldTemplateHints({
  value,
  variables,
  namesEvent = false,
  slug,
}: {
  value: string
  variables: Variable[]
  /** The governing scan rule builds the event's identity out of this field. */
  namesEvent?: boolean
  slug?: string
}) {
  // The shared copy hook, reported through a toast as the spec card does: the
  // bare `writeText` claimed "Copied" on plain HTTP and on a refused
  // permission, and the label never went away (EVT-49).
  const { copy } = useCopyToClipboard()
  const copyValue = async (text: string) => {
    if (await copy(text)) toast.success(`Copied ${text}`)
    else toast.error(`Could not copy ${text}`)
  }
  const resolved = resolveTemplateTokens(value, variables)
  if (resolved.length === 0) return null
  const unknown = resolved.filter(({ variable }) => variable === null)
  const documented = [
    ...new Map(
      resolved
        .filter(({ variable }) => variable !== null && (variable.allowed_values ?? []).length > 0)
        .map(({ variable }) => [variable!.id, variable!]),
    ).values(),
  ]
  if (!namesEvent && unknown.length === 0 && documented.length === 0) return null
  return (
    <div className="mt-1 space-y-1">
      {/* Validating the token and offering its documented values reads as a
          promise that the scanner will expand it. It will not:
          apply_scan_name_format substitutes {key} only, so the event is stamped
          with a literal ${…} identity that matches nothing. Say so where the
          mistake is made, and point at the mechanism that does collapse a
          family of legacy names onto one event. */}
      {namesEvent && (
        <p className="text-body-sm text-warning">
          This field names the event, so <span className="font-mono">{'${variable}'}</span> is
          stored literally and will not match a family of names.{' '}
          {slug ? (
            <Link to={projectPath(currentOrgSlug(), slug, '/settings/scans')} className="underline underline-offset-2">
              Group them with a scan event rule
            </Link>
          ) : (
            <span>Group them with a scan event rule</span>
          )}{' '}
          instead.
        </p>
      )}
      {unknown.map(({ token }) => (
        <p key={token} className="text-body-sm text-warning">Unknown property token: {token}</p>
      ))}
      {documented.map(variable => (
        <div key={variable.id} className="flex flex-wrap items-center gap-1">
          {variable.allowed_values.map(allowedValue => (
            <button
              key={allowedValue}
              type="button"
              aria-label={`Copy documented value ${allowedValue}`}
              className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
              onClick={() => void copyValue(allowedValue)}
            >
              {/* A code value, so the code token of the badge taxonomy (DS-6). */}
              <CodeToken className="cursor-pointer hover:bg-surface-hover">{allowedValue}</CodeToken>
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}

type FieldValueControlProps = {
  field: FieldDefinition
  value: string
  onChange: (value: string) => void
  variables: VariableSuggestion[]
  inputId?: string
  /**
   * Whether this row must be filled, when that is not simply `field.is_required`.
   * A column the event name is built from is required in practice — the form
   * blocks Create until it has a value — so the control has to say so too, or
   * the label's mark is a promise the browser never keeps.
   */
  requiredOverride?: boolean
  /**
   * The form flagged this row (an empty required value after Save). The form
   * is `noValidate`, so `required` is announced (`aria-required`) and checked
   * by the form, not by a browser bubble (AU-4).
   */
  invalid?: boolean
}

export function FieldValueControl({
  field,
  value,
  onChange,
  variables,
  inputId,
  requiredOverride,
  invalid: flagged = false,
}: FieldValueControlProps) {
  const required = requiredOverride ?? field.is_required
  if (field.field_type === 'boolean') {
    return (
      <SelectControl id={inputId} value={value} onChange={onChange} ariaRequired={required} aria-invalid={flagged}>
        <option value="">—</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </SelectControl>
    )
  }
  if (field.field_type === 'enum' && field.enum_options) {
    return (
      <SelectControl id={inputId} value={value} onChange={onChange} ariaRequired={required} aria-invalid={flagged}>
        <option value="">—</option>
        {field.enum_options.map(opt => <option key={opt} value={opt}>{opt}</option>)}
      </SelectControl>
    )
  }
  if (field.field_type === 'json') {
    return (
      <JsonEditor
        id={inputId}
        value={value}
        onChange={onChange}
        required={required}
        invalid={flagged}
        variables={variables}
      />
    )
  }
  if (field.field_type === 'number') {
    // Text, not a native number input: that one refused `$` and `{`, so the
    // variable autocomplete could never open, and it showed a stored `${price}`
    // as an empty box while the form still saved the token (EVT-23). The form
    // validates "a number or a ${variable}" instead, and blocks Save on
    // anything else.
    const invalid = !isNumberFieldValue(value)
    const errorId = inputId ? `${inputId}-number-error` : undefined
    return (
      <div>
        <VariableInput
          id={inputId}
          value={value}
          onChange={onChange}
          variables={variables}
          ariaRequired={required}
          type="text"
          inputMode="decimal"
          invalid={invalid || flagged}
          describedBy={invalid ? errorId : undefined}
        />
        {/* Red, not amber: a value that is neither a number nor a token
            blocks Save (AU-5). */}
        {invalid && (
          <FieldError
            id={errorId}
            className="mt-1 text-body-sm"
            message={
              <>
                Enter a number or a <span className="font-mono">{'${variable}'}</span> token.
              </>
            }
          />
        )}
      </div>
    )
  }
  return (
    <VariableInput
      id={inputId}
      value={value}
      onChange={onChange}
      variables={variables}
      ariaRequired={required}
      invalid={flagged}
      type={field.field_type === 'url' ? 'url' : 'text'}
    />
  )
}

export function MetaFieldControl({
  metaField,
  values,
  onChange,
  variables,
  inputId,
}: {
  metaField: MetaFieldDefinition
  /** Always a list: a single-valued field simply holds none or one. */
  values: string[]
  onChange: (values: string[]) => void
  variables: VariableSuggestion[]
  inputId?: string
}) {
  const value = values[0] ?? ''
  // Announced, not enforced: the label has always marked a required meta field,
  // and a screen reader heard only "star" (EVT-48). Native `required` would
  // start blocking saves the form has never blocked.
  const ariaRequired = metaField.is_required
  const setOne = (next: string) => onChange(next === '' ? [] : [next])
  // Several values, the way tags work — the interaction the analyst asked for
  // when an event picked up in a second task had nowhere to put the second Jira
  // key. The link template still applies per value: resolveMetaFieldHref is
  // already per-value, so N chips render N links with no new concept.
  if (metaField.allow_multiple) {
    const template = metaField.link_template
    return (
      <div>
        <ChipListInput
          inputId={inputId}
          values={values}
          // A whole address pasted into a chip is the same mistake as one
          // pasted into the single input, and the server strips per value —
          // so the chip shows the key that will be stored, not the address.
          onChange={next => {
            const keys = next.map(v => stripLinkTemplate(template, v))
            onChange(keys.filter((v, i) => keys.indexOf(v) === i))
          }}
          placeholder={metaFieldLinkExample(template) ? 'Type a key + Enter' : 'Type a value + Enter'}
          ariaLabel={`Add ${metaField.display_name}`}
        />
        {metaFieldLinkExample(template) && (
          <p className="mt-1 text-caption text-fg-tertiary">
            Enter the key, e.g. <span className="mono">{META_FIELD_LINK_EXAMPLE_KEY}</span> — each
            one opens on its own.
          </p>
        )}
      </div>
    )
  }
  if (metaField.field_type === 'boolean') {
    return (
      <SelectControl id={inputId} value={value} onChange={setOne} ariaRequired={ariaRequired}>
        <option value="">—</option>
        <option value="true">true</option>
        <option value="false">false</option>
      </SelectControl>
    )
  }
  if (metaField.field_type === 'enum' && metaField.enum_options) {
    return (
      <SelectControl id={inputId} value={value} onChange={setOne} ariaRequired={ariaRequired}>
        <option value="">—</option>
        {metaField.enum_options.map(opt => <option key={opt} value={opt}>{opt}</option>)}
      </SelectControl>
    )
  }
  // A pasted address is reduced to the key the template wraps, as the server
  // will do on write anyway (tripl-kjhi.5): the box then shows what is stored,
  // and the rendered link is never the template applied to a URL. On change
  // catches the paste; the blur catches a value that arrived any other way.
  const template = metaField.link_template
  const strip = (next: string) => (template ? stripLinkTemplate(template, next) : next)
  const example = metaFieldLinkExample(template)
  return (
    <div
      onBlur={() => {
        const settled = strip(value)
        if (settled !== value) setOne(settled)
      }}
    >
      <VariableInput
        id={inputId}
        value={value}
        onChange={next => setOne(strip(next))}
        variables={variables}
        ariaRequired={ariaRequired}
        type={metaField.field_type === 'url' ? 'url' : metaField.field_type === 'date' ? 'date' : 'text'}
      />
      {/* Said with the reader's own template: "uses link template with
          ${value}" named a mechanism and left the reader to work out that the
          box wants the key, not the link (tripl-kjhi.5). A template with no
          ${value} in it resolves no link at all, so there is nothing true to
          say about it here; the meta-field settings are where it gets fixed
          (AU-9). */}
      {example && (
        <p className="mt-1 text-caption text-fg-tertiary">
          Enter the key, e.g. <span className="mono">{META_FIELD_LINK_EXAMPLE_KEY}</span>
          {' — opens '}
          <span className="mono break-all">{example}</span>
        </p>
      )}
    </div>
  )
}

