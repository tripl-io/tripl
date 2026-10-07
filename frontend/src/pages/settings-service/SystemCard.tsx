import type { SystemSettings } from '@/types'
import { Dot } from '@/components/primitives/dot'
import { SCard } from '@/components/settings/kit'

/** The four judgements a row can carry. Narrower than DotTone on purpose. */
type SystemTone = 'neutral' | 'success' | 'warning' | 'danger'

type SystemRow = {
  label: string
  /** What the instance reports, in this row's own vocabulary. */
  value: string
  tone: SystemTone
  /**
   * Required, not optional. The tiles sit in a `grid`, which equalises the
   * height of every tile in a row, so the three annotated rows stretched the
   * four bare ones to their height and left ~70px of dead space under the word
   * "Configured" on each. Every row explains itself, in every
   * state it can be in — including the unknown branches of the schema row.
   */
  note: string
  /**
   * What is wrong, as a short clause for the "needs attention" summary. Set on
   * every warning or danger row: "<label> is <value>" said "Schema revision is
   * 0042_abcd." for a mismatch, which does not name the problem.
   */
  problem?: string
}

const VALUE_COLOR: Record<SystemTone, string> = {
  neutral: 'var(--fg-subtle)',
  success: 'var(--success)',
  warning: 'var(--warning)',
  danger: 'var(--danger)',
}

/**
 * A dependency the API cannot run without: unset is a failure, not a blank.
 *
 * @param use What the connection is for, shown once it is configured — the
 *   only thing left to say about a row that is already fine.
 */
function required(label: string, configured: boolean, use: string): SystemRow {
  return {
    label,
    value: configured ? 'Configured' : 'Unset',
    tone: configured ? 'success' : 'danger',
    note: configured ? use : 'Required — the API cannot reach this dependency.',
    problem: configured ? undefined : `${label} is unset`,
  }
}

/**
 * The one row read from the database rather than from the process environment.
 *
 * A serving app used to merely IMPLY that `alembic upgrade head` had run,
 * because compose gates it behind a `migrate` one-shot with
 * `service_completed_successfully`. That is an inference from a compose file,
 * not an observation, and it says nothing about a hand-rolled deploy — a
 * constraint-only migration changes nothing else a probe can see.
 *
 * Unknown is a real answer here and gets its own branch: a database whose
 * revision merely could not be read must not be painted as one whose migrations
 * were skipped. `up_to_date` is null for BOTH unknowns, though, so the branches
 * below split on which side is actually missing — an undeterminable head is not
 * an unreadable `alembic_version`, and only the second has nothing to report.
 */
function schemaRevisionRow(system: SystemSettings): SystemRow {
  const {
    alembic_revision: applied,
    alembic_head_revision: head,
    alembic_up_to_date: upToDate,
  } = system
  if (upToDate === true) {
    return {
      label: 'Schema revision',
      value: applied ?? '',
      tone: 'success',
      note: 'The database is at the newest migration this build ships.',
    }
  }
  if (upToDate === false) {
    return {
      label: 'Schema revision',
      // Both numbers, deliberately: what the database is at, and what this build
      // wants. Either alone leaves the operator unable to tell how far apart.
      value: applied ?? 'Unknown',
      tone: 'danger',
      problem: 'Schema revision does not match this build',
      // Direction is deliberately not asserted. The payload carries two revision
      // strings and no ordering between them, and this said "the migrate step
      // has not applied it" for both causes the admin guide itself lists: the
      // second is a rollback, where migrate DID run and applied something newer,
      // and pointing that operator at `alembic upgrade head` on the older image
      // only earns them "Can't locate revision".
      note: `This database is stamped with a revision that is not this build's head (${head}). Either the migrate step has not run here, or a newer release upgraded this database — migrations are forward-only.`,
    }
  }
  if (applied) {
    // Only the head is missing. The applied revision arrived in the payload, and
    // printing "Unknown" over it sent the operator to psql for a number already
    // on the tile.
    return {
      label: 'Schema revision',
      value: applied,
      tone: 'warning',
      problem: "Schema revision can't be compared with this build",
      note: "The database is stamped with this revision. This build's own migration head could not be determined, so the two cannot be compared.",
    }
  }
  return {
    label: 'Schema revision',
    value: 'Unknown',
    tone: 'warning',
    problem: 'Schema revision could not be read',
    note: head
      ? `Could not read alembic_version. This build ships ${head}.`
      : "Could not read alembic_version, and this build's own migration head could not be determined either.",
  }
}

/**
 * What this instance found in its environment at startup.
 *
 * Every row used to map `active -> success + pulse`, which rendered the two
 * rows that matter backwards: "Debug mode" on was a pulsing green "Configured"
 * while "Encryption key" unset was the same neutral grey as the optional OpenAI
 * fallback key. `active` means "set", not "healthy" — and for
 * `debug` it means the opposite — so each row now states its own judgement.
 *
 * The judgements are the ones backend/src/tripl/config.py already makes:
 *  - `debug` true returns early from `assert_production_ready` (config.py:286),
 *    skipping every startup check, and lets CORS fall back to "*"
 *    (config.py:263). On is a warning, not a pass.
 *  - an empty ENCRYPTION_KEY is one of those refused-boot problems: "data-source
 *    and alert-destination secrets would be stored as plaintext"
 *    (config.py:291). Unset is danger, not neutral.
 *  - REDIS_URL and OPENAI_API_KEY are the only two that are genuinely optional:
 *    cache.py no-ops without Redis, and the OpenAI key is a fallback for the AI
 *    and embedding keys (config.py:348-351). Unset is neutral for those two and
 *    for nothing else.
 */
function needsAttention(row: SystemRow): boolean {
  return row.tone === 'warning' || row.tone === 'danger'
}

function systemRows(system: SystemSettings): SystemRow[] {
  return [
    {
      label: 'Debug mode',
      value: system.debug ? 'On' : 'Off',
      tone: system.debug ? 'warning' : 'success',
      problem: system.debug ? 'Debug mode is on' : undefined,
      note: system.debug
        ? 'Production startup checks are skipped and CORS falls back to "*".'
        : 'Production startup checks ran at boot and CORS is limited to the configured origins.',
    },
    required(
      'Database URL',
      system.database_url_configured,
      'The async Postgres connection the API request path reads and writes through.',
    ),
    required(
      'Sync database URL',
      system.sync_database_url_configured,
      'The sync Postgres connection Celery tasks check their sessions out of.',
    ),
    required(
      'RabbitMQ URL',
      system.rabbitmq_url_configured,
      'The Celery broker every scan, alert and digest task is dispatched through.',
    ),
    {
      label: 'Redis URL',
      value: system.redis_url_configured ? 'Configured' : 'Unset',
      tone: system.redis_url_configured ? 'success' : 'neutral',
      note: system.redis_url_configured
        ? 'Backs the response cache and the live-update stream.'
        : 'Optional — caching and live updates stay off without it.',
    },
    {
      label: 'Encryption key',
      value: system.encryption_key_configured ? 'Configured' : 'Unset',
      tone: system.encryption_key_configured ? 'success' : 'danger',
      problem: system.encryption_key_configured ? undefined : 'Encryption key is unset',
      note: system.encryption_key_configured
        ? 'Data source and alert destination secrets are encrypted at rest with it.'
        : 'Data source and alert destination secrets are stored as plaintext.',
    },
    {
      label: 'OpenAI fallback key',
      value: system.openai_api_key_configured ? 'Configured' : 'Unset',
      tone: system.openai_api_key_configured ? 'success' : 'neutral',
      note: system.openai_api_key_configured
        ? 'Used when the AI or embedding key is blank.'
        : 'Optional — only used when the AI or embedding key is blank.',
    },
    schemaRevisionRow(system),
  ]
}

export function SystemCard({ system }: { system: SystemSettings }) {
  // The rows to act on first, so the owner does not scan all of them to find
  // the two that matter. The sort is stable: the rest keep their order.
  const rows = [...systemRows(system)].sort((a, b) => Number(needsAttention(b)) - Number(needsAttention(a)))
  const problems = rows.filter(needsAttention)

  return (
    // No card title: it repeated the page's own "System" h1.
    <SCard description="Read from this instance's environment when the API started. None of it can be changed from the app — set the variable where the process gets its environment, then restart. The schema revision is the exception: it is read from the database each time this page loads.">
      <div className="space-y-3 p-4">
        {problems.length > 0 && (
          <p className="text-body-sm font-medium text-fg">
            {problems.length === 1 ? '1 item needs attention' : `${problems.length} items need attention`}
            :{' '}
            {problems.map(row => row.problem ?? `${row.label} needs a look`).join(', ')}.
          </p>
        )}
        {/* Equal rows, so a two-line note does not make its row taller than
            the next. */}
        <div className="grid auto-rows-fr gap-2.5 sm:grid-cols-3">
          {rows.map(row => (
            <div
              key={row.label}
              className="flex flex-col gap-1.5 rounded-card border px-3 py-2.5"
              style={{
                // A tile that needs attention says so with its own border, not
                // only with a dot the eye skips scanning a grid of them.
                borderColor:
                  row.tone === 'warning' || row.tone === 'danger'
                    ? `color-mix(in oklab, var(--${row.tone}) 45%, var(--border))`
                    : 'var(--border-subtle)',
                background: 'var(--bg-sunken)',
              }}
            >
              <div className="flex items-center gap-1.5">
                {/* Pulse is for the rows an operator should act on, not for
                    every row that happens to be set. */}
                <Dot
                  tone={row.tone}
                  pulse={row.tone === 'warning' || row.tone === 'danger'}
                  size={7}
                />
                {/* Sentence case, not an uppercase eyebrow. */}
                <span className="text-caption font-medium text-fg-secondary">
                  {row.label}
                </span>
              </div>
              <span className="text-body-sm font-medium" style={{ color: VALUE_COLOR[row.tone] }}>
                {row.value}
              </span>
              <span className="text-caption leading-[1.4] text-fg-tertiary">
                {row.note}
              </span>
            </div>
          ))}
        </div>
      </div>
    </SCard>
  )
}
