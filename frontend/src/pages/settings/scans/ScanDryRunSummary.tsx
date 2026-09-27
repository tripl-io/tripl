import { Link } from 'react-router-dom'
import type { ScanDryRunNameWarning, ScanDryRunResponse } from '@/types'
import { duplicateEventPath, formatDuplicateScore } from '@/components/duplicates/duplicateHints'
import { formatDateTime } from '@/lib/datetime'
import { countOf } from '@/lib/plural'
import { dryRunNameExplosion, type NameExplosion, type NamingFixTarget } from './scanDryRunWarnings'

/**
 * "What this scan would create" — the answer the quick-start guide has promised
 * since it was written, rendered from the backend dry run (tripl-3y7z.6).
 *
 * The panel used to show five raw warehouse rows and name neither an event nor a
 * field, so the one question a user actually has before creating a scan — what
 * lands in my tracking plan? — was answered by running it and looking afterwards.
 *
 * Every number here is bounded, and says which bound it is under. The dry run
 * reports three partialities separately and this component must not collapse
 * them into a confident total:
 *  - the lookback window (an event absent from 24h is not an event that will not
 *    be created);
 *  - the sample cap (`sample_is_complete === false` ⇒ "at least N", never "N");
 *  - the 10,000-event generation cap.
 * There is deliberately no projected table-wide total anywhere below.
 */

/** How many names are listed inline. A 10,000-event plan must not be a DOM bomb. */
const MAX_LISTED = 20

const NAME_FORMAT_FIX =
  'Fix the event name format — a scan with this format fails every run.'

/**
 * The panel used to print the window sentence only when there WAS one, so a scan
 * with no time column silently read the whole base query while three docs and the
 * Limits section still described a bounded read. A missing bound is a fact about
 * the answer, so it is stated rather than omitted.
 */
const NO_WINDOW_NOTE = 'No time window — the whole base query was read'

/**
 * The read-nothing answer, stated rather than rendered as a wall of zeroes.
 *
 * With no rows the grouped analysis returns no group values, so the dry run has
 * no targets and genuinely knows nothing — not "no events would be created".
 * The two variants exist because the remedy differs: a bounded read points at
 * the window, an unbounded one can only point at the query.
 */
const EMPTY_WINDOW_NOTE =
  'The query returned no rows in this window. Widen Limits → Lookback (hours), or check that'
  + ' the time column is the one your table actually fills.'

const EMPTY_QUERY_NOTE = 'The query returned no rows, so there is nothing to describe yet.'

/**
 * Both remedies here are controls the product actually has.
 *
 * The note used to lead with "Raise the sample row limit". `sample_row_limit` is
 * a backend-only field: `toDryRunRequest` never sends it, no form field exposes
 * it, and it is always the default 5,000 — so the first thing the note told a
 * user to do could not be done, on the one panel built to be trusted. The
 * lookback is only offered when a window was actually applied, because without a
 * time column `resolve_lookback_window` returns None and Limits → Lookback
 * (hours) bounds nothing. Both strings match troubleshooting.md's two remedies.
 */
const TRUNCATED_LEAD = 'More distinct events exist than this preview looked at.'

const EVENTS_TRUNCATED_NOTE =
  `${TRUNCATED_LEAD} Narrow the base query to see them all.`

const EVENTS_TRUNCATED_WINDOWED_NOTE =
  `${TRUNCATED_LEAD} Narrow the base query, or shorten Limits → Lookback (hours),`
  + ' to see them all.'

/**
 * What "no new fields" means on the explicit-event-type path.
 *
 * `_dry_run_targets` sets `may_create_fields=False` when the config names an
 * Event type, so `fields` comes back empty on EVERY such answer — a run writes
 * only into the fields that event type already declares. The panel printed
 * "every column is already mapped" there, directly above its own Unmapped
 * columns list and a button offering to create those very fields: three
 * statements on one screen, two of them contradicting the first.
 *
 * `fields` empty AND `unmapped_columns` non-empty happens only on that path. The
 * grouped path creates a field for every unreserved column it does not already
 * have (`may_create_fields=True`), so it never leaves a column unmapped.
 */
const UNDECLARED_COLUMNS_LINE =
  'No new fields — a run only fills the fields this event type already declares.'
  + ' The columns it does not declare are listed below.'

/** True only when every column really is accounted for. */
const ALL_MAPPED_LINE = 'No new fields — every column is already mapped.'

/**
 * What a grouped scan's nameless group is called on screen.
 *
 * `analyze_cardinality_grouped` maps a NULL group value to the empty string, and
 * a genuine empty cell stringifies to the empty string too, so the dry run
 * carries `event_type: ""` for every event under those rows. Rendered verbatim
 * the chip is an empty span — invisible next to the labelled rows, so the one
 * row it was added to disambiguate is the one it fails to disambiguate.
 *
 * Named rather than hidden: the label is also the answer to "why are there two
 * `home` rows?" — some of your rows carry no type — which is a fact about the
 * user's data they can act on. The empty string stays on the wire, where it is
 * the real group name; only this screen gives it a reader's name.
 */
const NO_EVENT_TYPE_LABEL = '(no event type)'

/** `48%` — the share of the sample behind one event name. */
function share(value: number): string {
  const percent = value * 100
  return `${percent >= 1 || percent === 0 ? Math.round(percent) : percent.toFixed(1)}%`
}

function Note({ children, tone = 'muted' }: { children: React.ReactNode; tone?: 'muted' | 'warning' }) {
  return (
    <p
      className="m-0 text-caption leading-snug"
      style={{ color: tone === 'warning' ? 'var(--warning)' : 'var(--fg-subtle)' }}
    >
      {children}
    </p>
  )
}

function ColumnNote({ label, columns, explanation }: {
  label: string
  columns: string[]
  explanation: string
}) {
  if (columns.length === 0) return null
  return (
    <Note>
      <span className="font-medium">{label}:</span> {columns.join(', ')} — {explanation}
    </Note>
  )
}

/**
 * The name-format block. Rendered first and loudest: a format referencing a key
 * the rows cannot supply fails EVERY run of the config, so seeing it here rather
 * than after two hundred failed production runs is the highest-value thing the
 * dry run does (tripl-lpin).
 */
function NameFormatErrors({ errors }: { errors: string[] }) {
  if (errors.length === 0) return null
  return (
    <div
      role="alert"
      data-testid="dry-run-errors"
      className="rounded-lg border p-3 border-danger bg-danger-soft"
    >
      {errors.map(message => (
        <p key={message} className="m-0 text-body-sm font-medium text-danger">
          Event name format error: {message}
        </p>
      ))}
      <p className="m-0 mt-1 text-caption text-danger">
        {NAME_FORMAT_FIX}
      </p>
    </div>
  )
}

/**
 * The coaching callout for a draft that would swamp the plan (#247 DA-1). Same
 * box as the name-format errors, in warning rather than danger: the draft still
 * saves and runs; it just probably is not what its author meant. The bold terms
 * open the control that fixes it, where the caller can.
 */
function NameExplosionWarning({
  explosion,
  onFixNaming,
}: {
  explosion: NameExplosion
  onFixNaming?: (target: NamingFixTarget) => void
}) {
  const term = (label: string, target: NamingFixTarget) =>
    onFixNaming ? (
      <button
        type="button"
        className="font-semibold underline underline-offset-2"
        onClick={() => onFixNaming(target)}
      >
        {label}
      </button>
    ) : (
      <strong>{label}</strong>
    )
  return (
    <div
      data-testid="dry-run-explosion"
      className="rounded-card border p-3 text-body-sm border-warning bg-warning-soft text-fg"
    >
      <p className="m-0">
        This draft would add{' '}
        <strong>{countOf(explosion.newEvents, 'new event', 'new events')}</strong>
        {explosion.columns > 0
          ? `, one per combination of ${explosion.columns} columns.`
          : '.'}{' '}
        That is usually not what you want.
      </p>
      <p className="m-0 mt-1">
        Set an {term('Event name format', 'format')} (e.g. <code className="mono">{'{event_name}'}</code>) or
        add {term('Event groups', 'groups')} to name events from one column.
      </p>
    </div>
  )
}

/** How many sample names an explosion warning lists. */
const MAX_EXPLOSION_SAMPLES = 3

/**
 * What is wrong with the NAMES a run would create (F12, #265), from the dry
 * run's `name_warnings`: a slot that carries a high-cardinality value, and new
 * names that look like events already in the plan. Advisory — the draft still
 * saves and runs. The duplicate's name links to its page when the caller knows
 * the project (`slug`); otherwise it is plain text.
 */
function NameWarnings({ warnings, slug }: { warnings: ScanDryRunNameWarning[]; slug?: string }) {
  if (warnings.length === 0) return null
  const explosions = warnings.filter(warning => warning.code === 'combinatorial_explosion')
  const duplicates = warnings.flatMap(warning =>
    warning.code === 'duplicate' && warning.name && warning.duplicate_of
      ? [{ warning, name: warning.name, existing: warning.duplicate_of }]
      : [],
  )
  if (explosions.length === 0 && duplicates.length === 0) return null
  return (
    <div data-testid="dry-run-name-warnings" className="space-y-2">
      {explosions.map(warning => {
        const samples = (warning.samples ?? []).slice(0, MAX_EXPLOSION_SAMPLES)
        const where = warning.slot_label
          ?? (warning.slot != null ? `part ${warning.slot + 1}` : 'one part of the name')
        return (
          <div
            key={`explosion.${warning.event_type}.${warning.slot ?? ''}.${warning.pattern ?? ''}`}
            data-testid="dry-run-name-explosion"
            className="rounded-card border p-3 text-body-sm border-warning bg-warning-soft text-fg"
          >
            <p className="m-0">
              {warning.count != null ? (
                <strong>{countOf(warning.count, 'new name', 'new names')}</strong>
              ) : (
                <strong>Many new names</strong>
              )}
              {warning.event_type ? <> under {warning.event_type}</> : null} differ only in{' '}
              <strong>{where}</strong>
              {warning.pattern ? (
                <>
                  {' '}(<code className="mono">{warning.pattern}</code>)
                </>
              ) : null}
              . A high-cardinality value is part of the event name.
            </p>
            {samples.length > 0 && (
              <p className="m-0 mt-1 text-caption text-fg-secondary">
                For example:{' '}
                {samples.map((sample, index) => (
                  <span key={sample}>
                    {index > 0 && ', '}
                    <code className="mono">{sample}</code>
                  </span>
                ))}
              </p>
            )}
          </div>
        )
      })}
      {duplicates.length > 0 && (
        <div data-testid="dry-run-name-duplicates">
          <div className="text-body font-medium text-fg">
            {countOf(duplicates.length, 'new event looks', 'new events look')} like one already in
            your plan
          </div>
          <ul className="m-0 mt-1.5 list-none space-y-1 p-0">
            {duplicates.map(({ warning, name, existing }) => {
              return (
                <li
                  key={`duplicate.${warning.event_type}.${name}.${existing.event_id}`}
                  className="text-body-sm text-fg"
                >
                  <span className="font-medium">{name}</span>
                  <span className="text-fg-tertiary"> looks like </span>
                  {slug ? (
                    <Link
                      to={duplicateEventPath(slug, existing.event_id)}
                      className="font-medium underline underline-offset-2"
                    >
                      {existing.name}
                    </Link>
                  ) : (
                    <span className="font-medium">{existing.name}</span>
                  )}
                  <span className="text-caption text-fg-tertiary">
                    {' '}({formatDuplicateScore(existing.score)})
                  </span>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </div>
  )
}

function EventList({ events }: { events: ScanDryRunResponse['events'] }) {
  if (events.length === 0) return null
  const shown = events.slice(0, MAX_LISTED)
  const showEventType = new Set(events.map(event => event.event_type)).size > 1
  return (
    <>
      <ul className="m-0 mt-1.5 list-none space-y-1 p-0">
        {shown.map(event => (
          // An event is identified by its event type AND its name: a grouped
          // scan can produce the same name under two event types, and a run
          // creates both.
          <li
            key={`${event.event_type}.${event.source_name}`}
            className="flex items-baseline gap-2 text-body-sm"
          >
            <span className="min-w-0 flex-1 truncate font-medium text-fg">
              {event.name}
            </span>
            {/* Only when the answer spans more than one event type. A grouped
                scan names events from the columns left after the event-type
                column is reserved out, so the same name under two group values
                is ordinary — and two identical rows differing only in a row
                count read as double-counting rather than as two real events. */}
            {showEventType && (
              <span className="shrink-0 text-caption text-fg-tertiary">
                {event.event_type || NO_EVENT_TYPE_LABEL}
              </span>
            )}
            {event.grouped_by_rule && (
              <span className="shrink-0 text-caption text-fg-tertiary">
                merged by {event.grouped_by_rule}
              </span>
            )}
            <span className="shrink-0 text-caption text-fg-tertiary">
              {event.status === 'new' ? 'new' : 'already in your plan'}
            </span>
            <span
              className="shrink-0 text-caption tabular-nums text-fg-tertiary"
              title="Sampled warehouse rows behind this name."
            >
              {countOf(event.approx_row_count, 'row', 'rows')} · {share(event.share_of_sample)}
            </span>
          </li>
        ))}
      </ul>
      {events.length > shown.length && (
        <Note>
          Showing the {shown.length} most common of {events.length.toLocaleString()}.
        </Note>
      )}
    </>
  )
}

function FieldList({ fields }: { fields: ScanDryRunResponse['fields'] }) {
  if (fields.length === 0) return null
  const shown = fields.slice(0, MAX_LISTED)
  // A field belongs to an event type, and a draft spanning several repeats the
  // same names once per type: without the type the list read "event_name /
  // button_id / … / event_name" as a bug or as double counting (#247 DA-2).
  const showEventType = new Set(fields.map(field => field.event_type)).size > 1
  return (
    <>
      <ul className="m-0 mt-1.5 list-none space-y-1 p-0">
        {shown.map(field => (
          <li key={`${field.event_type}.${field.name}`} className="flex items-baseline gap-2 text-body-sm">
            <span className="min-w-0 flex-1 truncate font-medium text-fg">
              {field.name}
            </span>
            {showEventType && (
              <span className="max-w-[40%] shrink-0 truncate text-caption text-fg-tertiary">
                {field.event_type || NO_EVENT_TYPE_LABEL}
              </span>
            )}
            <span className="shrink-0 font-mono text-caption text-fg-tertiary">
              {field.type}
            </span>
            <span className="shrink-0 text-caption text-fg-tertiary">
              {field.status === 'new' ? 'new' : 'already in your plan'}
            </span>
          </li>
        ))}
      </ul>
      {fields.length > shown.length && (
        <Note>Showing {shown.length} of {fields.length.toLocaleString()}.</Note>
      )}
    </>
  )
}

export function ScanDryRunSummary({
  dryRun,
  onFixNaming,
  slug,
}: {
  dryRun: ScanDryRunResponse
  /** The project, so a duplicate warning can link to the planned event. */
  slug?: string
  /** Opens the naming control a warning names; omitted where there is none. */
  onFixNaming?: (target: NamingFixTarget) => void
}) {
  const events = dryRun.events
  const newEvents = events.filter(event => event.status === 'new').length
  const existingEvents = events.length - newEvents
  const newFields = dryRun.fields.filter(field => field.status === 'new')
  const newFieldTypes = new Set(newFields.map(field => field.event_type)).size
  const explosion = dryRunNameExplosion(dryRun)

  // "at least" is not decoration. The sample is the most common column
  // combinations, capped; when the cap was hit, the count is a floor and saying
  // a flat N would be the one lie this whole feature exists to avoid.
  const eventsHeadline = dryRun.sample_is_complete
    ? `Would create ${countOf(events.length, 'event', 'events')}`
    : `Would create at least ${countOf(events.length, 'event', 'events')}`

  const windowed = Boolean(dryRun.window_from && dryRun.window_to)
  // Not "no events" — no ANSWER. With no rows the grouped analysis returns no
  // group values, so the dry run has no targets and knows nothing about what a
  // run would write.
  const readNothing = dryRun.sampled_rows === 0
  const sampleSentence =
    `${countOf(dryRun.sampled_rows, 'row', 'rows')} in the`
    + ` ${dryRun.breakdown_combinations.toLocaleString()} most common column combinations.`

  return (
    <div data-testid="scan-dry-run-summary" className="space-y-3">
      <NameFormatErrors errors={dryRun.errors} />
      {explosion && <NameExplosionWarning explosion={explosion} onFixNaming={onFixNaming} />}
      <NameWarnings warnings={dryRun.name_warnings ?? []} slug={slug} />

      <div>
        <h4 className="m-0 text-body font-semibold text-fg">
          What this scan would create
        </h4>
        <Note>
          {windowed ? (
            <>From {formatDateTime(dryRun.window_from as string)} to{' '}
            {formatDateTime(dryRun.window_to as string)} · </>
          ) : (
            <>{NO_WINDOW_NOTE} · </>
          )}
          {sampleSentence}
        </Note>
      </div>

      {/* Read nothing, so there is nothing to say about events OR fields — the
          note REPLACES both bodies rather than sitting above them.

          Every line in those two bodies is a claim about rows this answer never
          saw: "Would create 0 events" reads as "this scan creates nothing", and
          the fields headline falls through to "every column is already mapped"
          — a run over a non-empty window would create a field for EVERY
          unreserved column on the grouped path. Gating only the events body
          would leave the second, and gating the events count only would leave a
          headline with no rows behind it. This is troubleshooting.md's
          documented "the window is empty" case (a stale table, a stopped
          backfill, a timestamp in the wrong unit): the remedy is the whole
          answer, and nothing else here is known. */}
      {readNothing ? (
        <Note tone="warning">
          {windowed ? EMPTY_WINDOW_NOTE : EMPTY_QUERY_NOTE}
        </Note>
      ) : (
        <>
          <div>
            <div className="flex flex-wrap items-baseline gap-x-2 text-body">
              <span className="font-medium text-fg">{eventsHeadline}</span>
              {events.length > 0 && (
                <span className="text-body-sm text-fg-tertiary">
                  · {newEvents} new · {existingEvents} already in your plan
                </span>
              )}
            </div>
            <EventList events={events} />
            {dryRun.events_truncated && (
              <Note>{windowed ? EVENTS_TRUNCATED_WINDOWED_NOTE : EVENTS_TRUNCATED_NOTE}</Note>
            )}
            {dryRun.max_events_reached && (
              <Note tone="warning">
                Stopped at {events.length.toLocaleString()} events. The real scan stops there too.
              </Note>
            )}
          </div>

          <div>
            <div className="text-body font-medium text-fg">
              {newFields.length > 0
                ? newFieldTypes > 1
                  ? `Would add ${countOf(newFields.length, 'field', 'fields')} across ${newFieldTypes} event types`
                  : `Would add ${countOf(newFields.length, 'field', 'fields')}`
                : dryRun.unmapped_columns.length > 0
                  ? UNDECLARED_COLUMNS_LINE
                  : ALL_MAPPED_LINE}
            </div>
            <FieldList fields={dryRun.fields} />
          </div>
        </>
      )}

      {dryRun.templated_columns.length > 0 && (
        <div className="space-y-1">
          {dryRun.templated_columns.map(templated => (
            <Note key={templated.column}>
              {templated.column} has more than {templated.threshold.toLocaleString()} distinct
              values, so its events are named with a {'{'}{templated.column}{'}'} template instead
              of one event per value.
            </Note>
          ))}
        </div>
      )}

      <div className="space-y-1">
        <ColumnNote
          label="Reserved columns"
          columns={dryRun.reserved_columns}
          explanation="tripl already uses these, so they never become event fields."
        />
        <ColumnNote
          label="Unmapped columns"
          columns={dryRun.unmapped_columns}
          explanation="no field matches them, so a run skips them too."
        />
        {dryRun.warnings.map(warning => (
          <Note key={warning}>{warning}</Note>
        ))}
      </div>
    </div>
  )
}
