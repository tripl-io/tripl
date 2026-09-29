import { Variable } from 'lucide-react'
import type { EventFieldVariableValue } from '@/types'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { formatDateTime } from '@/lib/datetime'

export function VariableValueContextTrigger({
  contexts,
}: {
  contexts?: EventFieldVariableValue[]
}) {
  const items = contexts ?? []
  if (items.length === 0) return null

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-fg-tertiary hover:bg-muted hover:text-foreground"
          aria-label="Observed property values"
        >
          <Variable className="h-3.5 w-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 space-y-3 p-3 text-body-sm">
        {items.map((context) => {
          // A context is the record that this variable is referenced here; the
          // values ride along with it and can be empty. That splits the empty
          // popover into two unrelated facts — this row holds no value, versus
          // values were counted but no example kept — which the single "No
          // examples stored" collapsed into one sentence that answered neither
          // (tripl-xv77.4). The wording below claims no more than this row
          // proves. An empty row cannot tell a column that is genuinely empty
          // from one whose values are recorded elsewhere, so it speaks about
          // this event's field and nothing wider, names no mechanism, and
          // promises no later fill.
          const nothingStored = context.observed_count === 0 && context.values.length === 0
          // The third fact this popover has to keep apart from the other two.
          // Excluding a variable stops scans sampling it but deletes nothing, so
          // a context can hold a real, correct, and permanently frozen reading.
          // Rendering it exactly like a live one would be the same defect as the
          // collapsed empty state: one appearance for two things a reader has to
          // act on differently.
          const isExcluded = context.excluded_from_scans === true
          // Formatted once, then used as both the guard and the text.
          // `formatDateTime` returns '' for a value it cannot parse, so the
          // empty string is what decides the row is not rendered — calling it
          // twice asked the same question twice and let the two answers drift.
          const lastRefreshed = context.updated_at ? formatDateTime(context.updated_at) : ''
          return (
            <div key={context.id} className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <CodeToken className="min-w-0">{`\${${context.variable_name}}`}</CodeToken>
                <div className="flex shrink-0 items-center gap-1">
                  {/* Its own badge rather than a replacement for the kind badge:
                      the kind still describes the stored values truthfully, and
                      losing "All values" would make a complete frozen set look
                      like a sample of one. */}
                  {isExcluded && (
                    <Chip size="xs">Excluded</Chip>
                  )}
                  {/* With nothing counted there is no sample for "Examples" to
                      name, so that case gets its own label. The counted-but-none-
                      kept case keeps "Examples": there the observation is real and
                      only the sample under the badge is missing. */}
                  <Chip size="xs" variant="outline">
                    {nothingStored
                      ? 'No values'
                      : context.value_kind === 'low'
                        ? 'All values'
                        : 'Examples'}
                  </Chip>
                </div>
              </div>
              <div className="text-fg-tertiary">
                {context.source_column} - {context.observed_count} observed
              </div>
              {/* "Refreshed", not "seen": the timestamp tracks the last WRITE.
                  A scan that re-observes nothing new leaves the row untouched,
                  so "last seen" would overstate it. Rendered on its own line so
                  the frozen-value sentences below stay the final word for an
                  excluded context. */}
              {lastRefreshed && (
                <div className="text-fg-tertiary">Last refreshed {lastRefreshed}</div>
              )}
              {context.values.length > 0 ? (
                <div className="flex max-h-36 flex-wrap gap-1 overflow-auto">
                  {context.values.map((value) => (
                    <CodeToken key={value} title={value}>
                      {value}
                    </CodeToken>
                  ))}
                </div>
              ) : nothingStored ? (
                <div className="text-fg-tertiary">
                  No value recorded for this field on this event
                </div>
              ) : (
                <div className="text-fg-tertiary">No examples stored</div>
              )}
              {/* Two sentences, because the line above them means two different
                  things. With values on screen the reader needs to know they are
                  last-seen and not current; with none, there is no value to date
                  and the honest statement is only that none will arrive. Neither
                  claims the values will be kept forever — Delete still removes
                  the variable and its rows with it. */}
              {isExcluded && (
                <div className="text-fg-tertiary">
                  {context.values.length > 0
                    ? 'Last seen before this property was excluded from scans — scans no longer refresh it.'
                    : 'This property is excluded from scans — scans no longer record values for it.'}
                </div>
              )}
            </div>
          )
        })}
      </PopoverContent>
    </Popover>
  )
}
