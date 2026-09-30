import { useId, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { IconButton } from '@/components/ui/icon-button'
import { cn } from '@/lib/utils'
import { TEXT_INPUT_CLASS } from './eventFormLayout'
import {
  cellText,
  cellToRaw,
  parseTemplateRows,
  rowProblem,
  serializeTemplateRows,
  tokenOf,
  type TemplateRow,
} from './jsonTemplateGrid'
import { VariableInput } from './VariableInput'
import type { VariableSuggestion } from './variableSuggestions'

/**
 * A JSON field value edited as rows of key → value (F23): the grid for the
 * common case, a flat object of property references and literals. A value
 * cell takes `${property}` (with the same autocomplete as any field), a number,
 * true/false/null, JSON, or plain text, which is stored as a string.
 *
 * The text is the model: every edit writes the whole template back through
 * `onChange`, and a change from outside (the JSON view, a reset) re-reads the
 * rows. Only render it for text `parseTemplateRows` accepts.
 */
export function JsonTemplateGrid({
  id,
  value,
  onChange,
  variables,
  invalid = false,
}: {
  /** Given to the first control, so the field's label still points at something. */
  id?: string
  value: string
  onChange: (next: string) => void
  variables: VariableSuggestion[]
  invalid?: boolean
}) {
  const uid = useId()
  const [rows, setRows] = useState<TemplateRow[]>(() => parseTemplateRows(value) ?? [])
  // Adjust-during-render, the JsonEditor idiom: only a value this grid did not
  // write itself re-reads the rows, so a half-typed duplicate key survives.
  const [seen, setSeen] = useState(value)
  if (value !== seen) {
    setSeen(value)
    if (value !== serializeTemplateRows(rows)) {
      const parsed = parseTemplateRows(value)
      if (parsed) setRows(parsed)
    }
  }

  const emit = (next: TemplateRow[]) => {
    setRows(next)
    const text = serializeTemplateRows(next)
    setSeen(text)
    onChange(text)
  }
  const update = (index: number, patch: Partial<TemplateRow>) =>
    emit(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  const addRow = () => {
    let n = rows.length + 1
    while (rows.some(row => row.key === `key_${n}`)) n++
    emit([...rows, { key: rows.length === 0 ? '' : `key_${n}`, raw: '""' }])
  }

  return (
    <div className="grid gap-1.5" role="group" aria-label="JSON keys">
      {rows.length > 0 && (
        <div className="hidden grid-cols-[minmax(0,2fr)_minmax(0,3fr)_2rem] gap-2 text-caption text-fg-tertiary sm:grid" aria-hidden="true">
          <span>Key</span>
          <span>Value</span>
          <span />
        </div>
      )}
      <ul className="grid gap-1.5">
        {rows.map((row, index) => {
          const problem = rowProblem(rows, index)
          const problemId = `${uid}-row-${index}-problem`
          const token = tokenOf(row.raw)
          const keyLabel = row.key || `row ${index + 1}`
          return (
            <li key={index} className="grid gap-0.5">
              <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_2rem] items-center gap-2">
                <input
                  id={index === 0 ? id : undefined}
                  aria-label={`Key of ${keyLabel}`}
                  className={cn(TEXT_INPUT_CLASS, 'font-mono')}
                  value={row.key}
                  placeholder="key"
                  spellCheck={false}
                  aria-invalid={problem || invalid ? true : undefined}
                  aria-describedby={problem ? problemId : undefined}
                  onChange={e => update(index, { key: e.target.value })}
                />
                <div className="min-w-0" title={token ? `The value of property ${token}` : undefined}>
                  <VariableInput
                    value={cellText(row.raw)}
                    onChange={text => update(index, { raw: cellToRaw(text) })}
                    variables={variables}
                    className={cn(token && 'font-mono text-primary')}
                    ariaLabel={`Value of ${keyLabel}`}
                  />
                </div>
                <IconButton
                  type="button"
                  variant="ghost"
                  className="size-8 text-fg-tertiary hover:text-destructive"
                  label={`Remove ${keyLabel}`}
                  onClick={() => emit(rows.filter((_, i) => i !== index))}
                >
                  <Trash2 className="size-3.5" aria-hidden="true" />
                </IconButton>
              </div>
              {problem && <p id={problemId} className="text-caption text-destructive">{problem}</p>}
            </li>
          )
        })}
      </ul>
      <div>
        <Button
          type="button"
          variant="outline"
          size="xs"
          onClick={addRow}
          id={rows.length === 0 ? id : undefined}
        >
          <Plus className="size-3" aria-hidden="true" />
          Add key
        </Button>
      </div>
    </div>
  )
}
