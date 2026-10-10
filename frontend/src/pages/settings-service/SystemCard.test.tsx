import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SystemSettings } from '@/types'
import { SystemCard } from './SystemCard'

/** A revision-shaped fixture value, deliberately not this repo's real head —
 *  nothing here should have to change when a migration lands. */
const APPLIED = 'abc123def456'
const NEWER = 'fed654cba321'

const HEALTHY: SystemSettings = {
  version: '9.8.7',
  edition: 'community',
  debug: false,
  database_url_configured: true,
  sync_database_url_configured: true,
  rabbitmq_url_configured: true,
  redis_url_configured: true,
  encryption_key_configured: true,
  openai_api_key_configured: true,
  alembic_revision: APPLIED,
  alembic_head_revision: APPLIED,
  alembic_up_to_date: true,
}

function renderCard(overrides: Partial<SystemSettings> = {}) {
  return render(<SystemCard system={{ ...HEALTHY, ...overrides }} />)
}

/** The tile whose uppercase label matches, so rows can be read independently. */
function tile(label: string): HTMLElement {
  const labelNode = screen.getByText(label)
  const element = labelNode.closest('div.flex-col')
  if (!element) throw new Error(`no tile around "${label}"`)
  return element as HTMLElement
}

describe('Instance System card', () => {
  /**
   * Every row used to render `active ? success : neutral`, so the two facts that
   * decide whether this instance is safe read backwards: debug mode ON was a
   * pulsing green "Configured", and a missing ENCRYPTION_KEY was the same
   * neutral "Unset" as the optional OpenAI fallback key.
   */
  it('does not report debug mode as a configured, healthy state', () => {
    renderCard({ debug: true })

    const row = tile('Debug mode')
    expect(within(row).queryByText('Configured')).toBeNull()
    expect(within(row).getByText('On')).toBeInTheDocument()
    expect(row).toHaveTextContent(/startup checks are skipped/i)
  })

  it('says what a missing encryption key costs, unlike a missing optional key', () => {
    renderCard({ encryption_key_configured: false, openai_api_key_configured: false })

    expect(tile('Encryption key')).toHaveTextContent(/secrets are stored as plaintext/i)
    // Same word, "Unset" — the difference has to be in what each row says next.
    expect(tile('OpenAI fallback key')).toHaveTextContent(/Optional/i)
    expect(tile('OpenAI fallback key')).not.toHaveTextContent(/plaintext/i)
  })

  it('marks an unset optional dependency optional rather than missing', () => {
    renderCard({ redis_url_configured: false })

    expect(tile('Redis URL')).toHaveTextContent(/Optional/i)
    expect(tile('Redis URL')).not.toHaveTextContent(/Required/i)
  })

  it('calls an unset core dependency required', () => {
    renderCard({ rabbitmq_url_configured: false })

    expect(tile('RabbitMQ URL')).toHaveTextContent(/Required/i)
  })

  /**
   * Only the three rows that needed action carried a note, so on a healthy
   * instance four of the seven tiles said nothing beyond "Configured" — and
   * because the tiles are grid cells, the annotated ones stretched the bare
   * ones to their height and left roughly 70px of dead space in each.
   * This is a property of the row data, not of the CSS: every
   * row explains itself in whatever state it is in.
   */
  it.each([
    ['Debug mode', 'Off'],
    ['Database URL', 'Configured'],
    ['Sync database URL', 'Configured'],
    ['RabbitMQ URL', 'Configured'],
    ['Redis URL', 'Configured'],
    ['Encryption key', 'Configured'],
    ['OpenAI fallback key', 'Configured'],
    ['Schema revision', APPLIED],
  ])('explains %s on a healthy instance, not just the rows that need action', (label, value) => {
    renderCard()

    const row = tile(label)
    expect(within(row).getByText(value)).toBeInTheDocument()
    // Whatever the tile says past its label and its one-word value.
    const explanation = (row.textContent ?? '').replace(label, '').replace(value, '').trim()
    expect(explanation).not.toBe('')
  })

  /**
   * The page promised "build information" and showed none: every bug report
   * after launch needs the version, and the API already knew it.
   */
  it('leads with the version and edition this instance runs', () => {
    renderCard({ edition: 'enterprise' })

    const row = tile('Version')
    expect(within(row).getByText('9.8.7 · Enterprise')).toBeInTheDocument()
    expect(row).toHaveTextContent(/bug reports/i)
  })

  /**
   * The card says "set the variable", so a tile that needs action has to say
   * which one: Debug and Encryption key never named DEBUG or ENCRYPTION_KEY.
   */
  it('names the variable to set on every tile that needs action', () => {
    renderCard({ debug: true, encryption_key_configured: false, database_url_configured: false })

    expect(tile('Debug mode')).toHaveTextContent(/Set DEBUG=false and restart/)
    expect(tile('Encryption key')).toHaveTextContent(/Set ENCRYPTION_KEY and restart/)
    expect(tile('Database URL')).toHaveTextContent(/Set DATABASE_URL and restart/)
  })

  it.each([
    ['Debug mode', 'DEBUG'],
    ['Database URL', 'DATABASE_URL'],
    ['Sync database URL', 'SYNC_DATABASE_URL'],
    ['RabbitMQ URL', 'RABBITMQ_URL'],
    ['Redis URL', 'REDIS_URL'],
    ['Encryption key', 'ENCRYPTION_KEY'],
    ['OpenAI fallback key', 'OPENAI_API_KEY'],
  ])('names the variable behind %s on a healthy instance too', (label, env) => {
    renderCard()

    expect(within(tile(label)).getByText(env)).toBeInTheDocument()
  })

  it('says these values are read from the environment, not editable here', () => {
    renderCard()

    expect(screen.getByText(/set the variable where the process gets its environment/i))
      .toBeInTheDocument()
  })

  /**
   * A serving app used to only IMPLY that `alembic upgrade head` had run: compose
   * gates it behind a `migrate` one-shot with `service_completed_successfully`.
   * That inference holds only while prod uses that compose file, and a
   * constraint-only migration changes nothing else a probe can see, so a
   * hand-rolled deploy that skipped migrations looked identical to a correct one.
   */
  describe('schema revision', () => {
    it('reports the revision the database is stamped with when it matches head', () => {
      renderCard()

      const row = tile('Schema revision')
      expect(within(row).getByText(APPLIED)).toBeInTheDocument()
      expect(row).toHaveTextContent(/newest migration this build ships/i)
      expect(row).not.toHaveTextContent(/Unknown/i)
    })

    it('shows both numbers on a mismatch without asserting which side is behind', () => {
      // What the database is at AND what this build wants: either alone leaves
      // the operator unable to tell how far apart they are.
      //
      // What the payload cannot say is WHICH is behind — nothing compares the
      // applied revision against the build's script directory — and the admin
      // guide lists two causes for this state. The second is a rollback, where
      // the migrate step ran and applied something NEWER, so "the migrate step
      // has not applied it" is false exactly half the time.
      renderCard({ alembic_head_revision: NEWER, alembic_up_to_date: false })

      const row = tile('Schema revision')
      expect(within(row).getByText(APPLIED)).toBeInTheDocument()
      expect(row).toHaveTextContent(NEWER)
      expect(row).toHaveTextContent(/migrations are forward-only/i)
      expect(row).not.toHaveTextContent(/the migrate step has not applied it/i)
    })

    it('says Unknown rather than guessing when the revision cannot be read', () => {
      // Unreadable is not the same as behind. Painting it as behind is exactly
      // the false alarm this tile exists to avoid.
      renderCard({ alembic_revision: null, alembic_up_to_date: null })

      const row = tile('Schema revision')
      expect(within(row).getByText('Unknown')).toBeInTheDocument()
      expect(row).toHaveTextContent(/Could not read alembic_version/i)
      expect(row).not.toHaveTextContent(/migrations are forward-only/i)
    })

    it('still names the applied revision when only this build’s head is unknown', () => {
      // `alembic_up_to_date` is null for both unknowns, so branching on it alone
      // printed "Unknown" over a revision the response had carried — and sent
      // the operator to psql for a number already on the tile.
      renderCard({ alembic_head_revision: null, alembic_up_to_date: null })

      const row = tile('Schema revision')
      expect(within(row).getByText(APPLIED)).toBeInTheDocument()
      expect(row).not.toHaveTextContent('Unknown')
      expect(row).toHaveTextContent(/migration head could not be determined/i)
      // The comparison genuinely cannot be made, so the tone stays warning — but
      // that is not a reason to withhold the half that IS known.
      expect(row).not.toHaveTextContent(/Could not read alembic_version/i)
    })

    it('still explains itself when neither side could be read', () => {
      renderCard({
        alembic_revision: null,
        alembic_head_revision: null,
        alembic_up_to_date: null,
      })

      const row = tile('Schema revision')
      expect(within(row).getByText('Unknown')).toBeInTheDocument()
      const explanation = (row.textContent ?? '')
        .replace('Schema revision', '')
        .replace('Unknown', '')
        .trim()
      expect(explanation).not.toBe('')
    })

    it('says this one tile is read from the database, not from the environment', () => {
      renderCard()

      expect(screen.getByText(/read from the database each time this page loads/i))
        .toBeInTheDocument()
    })
  })
})

describe('Instance System card layout', () => {
  it('names the problems first instead of leaving them to be found among the tiles', () => {
    renderCard({ debug: true, encryption_key_configured: false })

    expect(
      screen.getByText('2 items need attention: Debug mode is on, Encryption key is unset.'),
    ).toBeInTheDocument()
    // No second "System" title under the page's own.
    expect(screen.queryByRole('heading', { name: 'System' })).toBeNull()
  })

  it('names what is wrong with the schema, not its revision id', () => {
    renderCard({ alembic_head_revision: NEWER, alembic_up_to_date: false })

    expect(
      screen.getByText('1 item needs attention: Schema revision does not match this build.'),
    ).toBeInTheDocument()
  })

  it('says an unreadable schema revision could not be read', () => {
    renderCard({ alembic_revision: null, alembic_head_revision: NEWER, alembic_up_to_date: null })

    expect(
      screen.getByText('1 item needs attention: Schema revision could not be read.'),
    ).toBeInTheDocument()
  })

  it('says nothing needs attention by saying nothing', () => {
    renderCard()

    expect(screen.queryByText(/need(s)? attention/)).toBeNull()
  })
})
