import { useId, useMemo, useRef, useState, type KeyboardEvent } from "react"
import { renderTemplatePreview } from './templatePreview'
import type { AlertDestinationType, AlertMessageFormat } from "@/types"
import { CodeToken } from "@/components/primitives/code-token"
import { AnchoredListbox } from "@/components/ui/anchored-listbox"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { FORMAT_HELP, MESSAGE_FORMAT_OPTIONS, findTemplateVariableToken, unknownTemplateVariables } from "./constants"
import { invalidAria } from "@/components/forms/validation"
import { fieldErrorId } from "@/lib/fieldErrors"
import { pluralize } from '@/lib/plural'

export function TemplateEditor({
  destinationType,
  messageFormat,
  onMessageFormatChange,
  title,
  variableOptions,
  helperText,
  showFormatSelector,
  placeholder,
  value,
  onChange,
  error,
}: {
  /**
   * The channel the rule routes to, or null before one is picked. The format
   * choices are per channel, so none are offered until there is one: falling
   * back to the first destination showed formats for a channel the reader had
   * never chosen.
   */
  destinationType: AlertDestinationType | null
  messageFormat: AlertMessageFormat
  onMessageFormatChange: (value: AlertMessageFormat) => void
  title: string
  variableOptions: readonly { name: string; description: string }[]
  helperText: string
  showFormatSelector?: boolean
  placeholder: string
  value: string
  onChange: (value: string) => void
  /** A server rejection that names this template. */
  error?: string | null
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const fieldRef = useRef<HTMLDivElement | null>(null)
  const [activeToken, setActiveToken] = useState<{ start: number; end: number; query: string } | null>(null)
  // Which suggestion the arrow keys are on. The textarea declared
  // role="combobox" and handled none of the keys that role promises, so a
  // keyboard user could see the suggestions and not reach them.
  const [activeIndex, setActiveIndex] = useState(0)
  // One id per instance. The rule dialog renders two of these, and a fixed
  // `id="msg-format-label"` gave the document two elements with one id, so
  // `aria-labelledby` resolved to the first for both.
  const instanceId = useId()
  const formatLabelId = `${instanceId}-format-label`
  // Per instance too, for the same reason as the label: the title is not unique.
  const textareaId = `${instanceId}-template`
  const listboxId = `${textareaId}-suggestions`
  const optionId = (index: number) => `${listboxId}-${index}`

  const suggestions = useMemo(() => {
    if (!activeToken) return []
    const needle = activeToken.query.toLowerCase()
    return variableOptions.filter(option =>
      !needle || option.name.toLowerCase().includes(needle),
    ).slice(0, 8)
  }, [activeToken, variableOptions])
  const listOpen = activeToken !== null && suggestions.length > 0
  // Clamped at read time, so a list that shrank as the query grew cannot
  // leave the highlight past its end.
  const highlighted = Math.min(activeIndex, Math.max(suggestions.length - 1, 0))

  // Checked against the same list the suggestions come from, so a typo such
  // as `${scope_nme}` — or an item variable in the message template — is named
  // under this editor while typing, not as a 422 after submit. A
  // warning rather than a block: the backend owns the final word, and a list
  // that fell behind it must not lock a valid template out.
  const unknownVariables = useMemo(
    () => unknownTemplateVariables(value, variableOptions),
    [value, variableOptions],
  )
  const warningId = `${textareaId}-unknown`
  const describedBy = [
    unknownVariables.length > 0 ? warningId : null,
    error ? fieldErrorId(textareaId) : null,
  ].filter(Boolean).join(' ') || undefined

  const updateToken = (nextValue: string, cursor: number) => {
    const next = findTemplateVariableToken(nextValue, cursor)
    // A different query is a different list; start it from the top.
    if (next?.query !== activeToken?.query) setActiveIndex(0)
    setActiveToken(next)
  }

  const insertVariable = (variableName: string) => {
    const textarea = textareaRef.current
    const currentValue = value
    const fallbackPosition = textarea?.selectionStart ?? currentValue.length
    const start = activeToken?.start ?? fallbackPosition
    const end = activeToken?.end ?? fallbackPosition
    const insertion = `\${${variableName}}`
    const nextValue = currentValue.slice(0, start) + insertion + currentValue.slice(end)
    onChange(nextValue)
    setActiveToken(null)

    requestAnimationFrame(() => {
      const nextCursor = start + insertion.length
      textarea?.focus()
      textarea?.setSelectionRange(nextCursor, nextCursor)
    })
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!listOpen) return
    const last = suggestions.length - 1
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        setActiveIndex(highlighted >= last ? 0 : highlighted + 1)
        return
      case 'ArrowUp':
        event.preventDefault()
        setActiveIndex(highlighted <= 0 ? last : highlighted - 1)
        return
      case 'Enter':
      case 'Tab': {
        const option = suggestions[highlighted]
        if (!option) return
        event.preventDefault()
        insertVariable(option.name)
        return
      }
      case 'Escape':
        // The dialog around this editor stands down for an open list (see
        // RuleEditorDialog's `onEscapeKeyDown`), so Escape closes the list
        // that covers the helper text, not the whole form.
        event.preventDefault()
        setActiveToken(null)
        return
    }
  }

  const formatSelector = showFormatSelector !== false
  const [previewing, setPreviewing] = useState(false)
  const preview = useMemo(
    () => (previewing ? renderTemplatePreview(value || placeholder) : ''),
    [previewing, value, placeholder],
  )
  return (
    <div className="grid gap-2">
      {/* One header row: the title, the format beside it, and the two tools.
          The format used to own a 220px side column that — before a
          destination was picked, and always on the item template — held only
          a grey note stretched to the textarea's height. */}
      <div className="flex flex-wrap items-center gap-2">
        <Label htmlFor={textareaId} className="mr-auto">{title}</Label>
        {formatSelector && destinationType && (
          <span className="inline-flex items-center gap-1.5">
            <Label id={formatLabelId} className="text-body-sm font-normal text-fg-subtle">Message format</Label>
            <Select
              value={messageFormat}
              onValueChange={nextValue => onMessageFormatChange(nextValue as AlertMessageFormat)}
            >
              <SelectTrigger aria-labelledby={formatLabelId} className="h-7 w-auto min-w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MESSAGE_FORMAT_OPTIONS[destinationType].map(option => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </span>
        )}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-pressed={previewing}
          onClick={() => setPreviewing(current => !current)}
        >
          Preview
        </Button>
        <Popover>
          <PopoverTrigger asChild>
            <Button type="button" variant="outline" size="sm">Variables</Button>
          </PopoverTrigger>
          {/* Capped at the viewport: 28rem is 448px, wider than a 375px
              phone, and Radix clamps the position but not the width, so
              the list ran off screen with its descriptions cut. */}
          <PopoverContent align="end" className="w-[min(28rem,calc(100vw-2rem))] space-y-2">
            <div className="text-body font-medium">Available variables</div>
            <div className="max-h-72 overflow-y-auto space-y-1">
              {variableOptions.map(option => (
                <button
                  key={option.name}
                  type="button"
                  className="flex w-full items-start justify-between gap-3 rounded-md px-2 py-1.5 text-left text-body hover:bg-muted"
                  onClick={() => insertVariable(option.name)}
                >
                  <span className="font-mono text-body-sm">{`\${${option.name}}`}</span>
                  <span className="text-body-sm text-fg-tertiary">{option.description}</span>
                </button>
              ))}
            </div>
          </PopoverContent>
        </Popover>
      </div>
      {formatSelector && !destinationType && (
        <p className="m-0 text-caption text-fg-subtle">
          Pick a destination to choose a message format — the choices depend on the channel.
        </p>
      )}
      <div className="grid gap-2">
        {previewing && (
          <div
            role="region"
            aria-label={`${title} preview`}
            className="max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded-control border border-border-subtle bg-bg-sunken p-3 text-body-sm"
          >
            {preview}
            <p className="mt-2 mb-0 text-caption text-fg-subtle">
              Sample values; formatting marks are shown as typed.
            </p>
          </div>
        )}
        <div ref={fieldRef} className={previewing ? 'hidden' : 'relative'}>
          <Textarea
            ref={textareaRef}
            id={textareaId}
            role="combobox"
            aria-expanded={listOpen}
            aria-autocomplete="list"
            aria-controls={listboxId}
            aria-activedescendant={listOpen ? optionId(highlighted) : undefined}
            {...invalidAria(textareaId, error)}
            aria-describedby={describedBy}
            value={value}
            rows={8}
            placeholder={placeholder}
            onChange={event => {
              onChange(event.target.value)
              updateToken(event.target.value, event.target.selectionStart ?? event.target.value.length)
            }}
            onKeyDown={handleKeyDown}
            onClick={event => updateToken(event.currentTarget.value, event.currentTarget.selectionStart ?? event.currentTarget.value.length)}
            onKeyUp={event => {
              // The list's own keys have been handled on the way down; a
              // re-read here would re-open the list Escape just closed.
              if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(event.key)) return
              updateToken(event.currentTarget.value, event.currentTarget.selectionStart ?? event.currentTarget.value.length)
            }}
            onBlur={() => setActiveToken(null)}
          />
          {/* Portalled and anchored to the field: inline, the list
              sat under the dialog's scroll clip and z-order. */}
          <AnchoredListbox
            id={listboxId}
            open={listOpen}
            anchorRef={fieldRef}
            onDismiss={() => setActiveToken(null)}
            ariaLabel="Variable suggestions"
            className="max-h-72"
          >
              {suggestions.map((option, index) => (
                <button
                  key={option.name}
                  type="button"
                  id={optionId(index)}
                  role="option"
                  aria-selected={index === highlighted}
                  // Out of the Tab order: the list is driven from the
                  // textarea through aria-activedescendant.
                  tabIndex={-1}
                  className={`flex w-full items-start justify-between gap-3 rounded-sm px-2 py-1.5 text-left hover:bg-surface-hover ${index === highlighted ? 'bg-surface-hover' : ''}`}
                  // Keep focus in the textarea, where the arrow keys live.
                  onMouseDown={event => event.preventDefault()}
                  onClick={() => insertVariable(option.name)}
                >
                  <span className="font-mono text-body-sm">{`\${${option.name}}`}</span>
                  <span className="text-body-sm text-fg-tertiary">{option.description}</span>
                </button>
              ))}
          </AnchoredListbox>
        </div>
        {unknownVariables.length > 0 && (
          <p id={warningId} className="text-body-sm text-warning">
            {pluralize(unknownVariables.length, 'Unknown variable', 'Unknown variables')}{' '}
            {unknownVariables.map(name => `\${${name}}`).join(', ')} — this template does not
            offer {pluralize(unknownVariables.length, 'it', 'them')}, so saving will be refused.
            Pick from Variables.
          </p>
        )}
        {error && (
          <p id={fieldErrorId(textareaId)} className="text-body-sm text-destructive">{error}</p>
        )}
        <p className="m-0 text-caption text-fg-subtle">
          {helperText}
        </p>
        {formatSelector && destinationType && messageFormat !== 'plain' && (
          <p className="m-0 flex flex-wrap items-center gap-1 text-caption text-fg-subtle">
            <span>Formatting:</span>
            {FORMAT_HELP[messageFormat].filter(line => !line.includes(' ')).map(helpLine => (
              <CodeToken key={helpLine}>{helpLine}</CodeToken>
            ))}
            {FORMAT_HELP[messageFormat].filter(line => line.includes(' ')).map(helpLine => (
              <span key={helpLine}>{helpLine}</span>
            ))}
          </p>
        )}
      </div>
    </div>
  )
}
