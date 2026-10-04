import { describe, expect, it } from 'vitest'
import { friendlyScanError } from './scanError'

// Realistic raw exceptions as they arrive from the backend, host/port intact.
const CLICKHOUSE_TIMEOUT =
  "HTTPSConnectionPool(host='clickhouse.internal', port=8443): Read timed out. (read timeout=30)"
const SQLALCHEMY_AUTOFLUSH =
  "InvalidRequestError: This Session's transaction has been rolled back; " +
  'use a session.no_autoflush block to avoid autoflush during this query. ' +
  '(Background on this error at: https://sqlalche.me/e/20/) SQLAlchemy'
const CONNECTION_REFUSED = 'ConnectionRefusedError: [Errno 111] Connection refused'
const GETADDRINFO_FAILURE =
  "socket.gaierror: [Errno -2] getaddrinfo failed for host='db.example.com', port=9000"

/** A user-facing message must leak no host, no port number, no library, no ORM hint. */
function expectSanitised(message: string) {
  expect(message).not.toMatch(/\d/) // no port numbers
  expect(message.toLowerCase()).not.toContain('sqlalchemy')
  expect(message.toLowerCase()).not.toContain('autoflush')
  expect(message.toLowerCase()).not.toContain('host')
  expect(message.toLowerCase()).not.toContain('port')
  expect(message.toLowerCase()).not.toContain('httpsconnectionpool')
  expect(message.toLowerCase()).not.toContain('getaddrinfo')
}

describe('friendlyScanError', () => {
  it('maps a ClickHouse HTTPSConnectionPool read-timeout to the timeout message', () => {
    const result = friendlyScanError(CLICKHOUSE_TIMEOUT)
    expect(result.message).toBe('Scan failed: the data source did not respond in time.')
    expect(result.technical).toBe(CLICKHOUSE_TIMEOUT)
    expectSanitised(result.message)
  })

  it('maps a bare "Read timed out" string to the timeout message', () => {
    expect(friendlyScanError('Read timed out').message).toBe(
      'Scan failed: the data source did not respond in time.',
    )
  })

  it('maps a generic "timeout" string to the timeout message', () => {
    expect(friendlyScanError('upstream request timeout').message).toBe(
      'Scan failed: the data source did not respond in time.',
    )
  })

  it('maps a SQLAlchemy autoflush hint to the internal-error message', () => {
    const result = friendlyScanError(SQLALCHEMY_AUTOFLUSH)
    expect(result.message).toBe('Scan failed: an internal error occurred while saving results.')
    expect(result.technical).toBe(SQLALCHEMY_AUTOFLUSH)
    expectSanitised(result.message)
  })

  it('maps a bare no_autoflush hint to the internal-error message', () => {
    expect(friendlyScanError('with session.no_autoflush:').message).toBe(
      'Scan failed: an internal error occurred while saving results.',
    )
  })

  it('maps a "Connection refused" string to the connect message', () => {
    const result = friendlyScanError(CONNECTION_REFUSED)
    expect(result.message).toBe('Scan failed: could not connect to the data source.')
    expect(result.technical).toBe(CONNECTION_REFUSED)
    expectSanitised(result.message)
  })

  it('maps a getaddrinfo / host= / port= failure to the connect message', () => {
    const result = friendlyScanError(GETADDRINFO_FAILURE)
    expect(result.message).toBe('Scan failed: could not connect to the data source.')
    expect(result.technical).toBe(GETADDRINFO_FAILURE)
    expectSanitised(result.message)
  })

  it('passes the event-name-format failure through verbatim', () => {
    // The real string from core/analyzers/event_generator._apply_name_format.
    // It only survives because it starts with "Scan failed" — this test fails if
    // anyone drops that prefix, which would silently return the operator to the
    // bare "Scan failed." that hid a four-day outage.
    const raw =
      'Scan failed: the event name format references unknown keys: action. ' +
      'Available keys: platform, screen_name, time'
    const result = friendlyScanError(raw)
    expect(result.message).toBe(raw)
    expect(result.technical).toBeUndefined()
  })

  it('passes it through even when a warehouse column name embeds a raw-internals marker', () => {
    // The curated message enumerates the warehouse's OWN column names, and real
    // tables have columns like `client_errno` and `error_traceback_id`. As bare
    // substrings those read as a raw exception, the verbatim pass-through was
    // skipped, and `connection_id` two columns later then mapped the whole thing
    // to an actively WRONG connection error.
    const raw =
      'Scan failed: the event name format references unknown keys: action. ' +
      'Available keys: client_errno, connection_id, error_traceback_id, sqlalchemy_version'
    const result = friendlyScanError(raw)
    expect(result.message).toBe(raw)
    expect(result.technical).toBeUndefined()
  })

  it('still catches the markers when they stand alone in raw exception text', () => {
    // The other half of the bargain: token boundaries must not blunt the guard.
    for (const raw of [
      'Scan failed: ConnectionRefusedError: [Errno 111] Connection refused',
      'Scan failed: Traceback (most recent call last):',
      'Scan failed: SQLAlchemy could not complete the statement',
      'Scan failed: use a session.no_autoflush block',
      'Scan failed: <Engine object at 0x7f3d10a2b4c0> died',
    ]) {
      expect(friendlyScanError(raw).message).not.toBe(raw)
      expect(friendlyScanError(raw).technical).toBe(raw)
    }
  })

  it('falls back to a bare "Scan failed." for unknown errors but still keeps the technical detail', () => {
    const raw = 'KeyError: tracking_plan_id'
    const result = friendlyScanError(raw)
    expect(result.message).toBe('Scan failed.')
    expect(result.technical).toBe(raw)
  })

  it('passes a backend-sanitised "Scan failed: …" message through verbatim', () => {
    // After the backend sanitises failures (issue H3), error_message reads e.g.
    // "Scan failed: could not connect to the data source." The phrasing does not
    // match the raw-exception patterns, so it must be shown as-is rather than
    // collapsed to a generic "Scan failed.".
    for (const msg of [
      'Scan failed: could not connect to the data source.',
      'Scan failed: the data source did not respond in time.',
      'Scan failed due to an internal error. Please try again or contact support.',
    ]) {
      const result = friendlyScanError(msg)
      expect(result.message).toBe(msg)
      expect(result.technical).toBeUndefined()
    }
  })

  it('passes the backend CURATED messages through, not just the generic three', () => {
    // The test above uses the three generic summaries, and every one of them
    // happens to carry the prefix — which is precisely how tripl-7bol survived
    // a green suite. The curated messages are the ones the mechanism exists
    // for, and not one of them carried it: each arrived here intact and was
    // collapsed into the bare 'Scan failed.' it had been written to replace.
    //
    // These read as `user_facing_error` now emits them
    // (backend/src/tripl/worker/tasks/_errors.py), which prefixes every curated
    // message so no raise site has to remember to.
    for (const msg of [
      'Scan failed: The scan query reached the configured row limit (50000); increase scan_row_limit to avoid partial generation',
      "Scan failed: This scan has no Event type and no Event type column, so it cannot "
        + "name any events. Set one under the scan's Configuration tab.",
      'Scan failed: The scan config has no event group rules',
      'Scan failed: Fact metric aggregate reached the metric query row limit (100000) for chunk 2026-08-01T00:00:00..2026-08-01T01:00:00; narrow the metric breakdown',
    ]) {
      const result = friendlyScanError(msg)
      expect(result.message).toBe(msg)
      expect(result.technical).toBeUndefined()
    }
  })

  it('does NOT pass through a "Scan failed:" string that still carries raw internals', () => {
    // Defensive: even if a path prefixes raw text, the raw-internals guard forces
    // it back through the mapping rules so host/port never leak.
    const sneaky = "Scan failed: HTTPSConnectionPool(host='db', port=8443): boom"
    const result = friendlyScanError(sneaky)
    expect(result.message).toBe('Scan failed: the data source did not respond in time.')
    expect(result.technical).toBe(sneaky)
    expectSanitised(result.message)
  })

  it('returns no technical detail for nullish input', () => {
    for (const raw of [null, undefined, '', '   ']) {
      const result = friendlyScanError(raw)
      expect(result.message).toBe('Scan failed.')
      expect(result.technical).toBeUndefined()
    }
  })

  it('never leaks a hostname, port, library name or ORM hint in the message', () => {
    for (const raw of [CLICKHOUSE_TIMEOUT, SQLALCHEMY_AUTOFLUSH, CONNECTION_REFUSED, GETADDRINFO_FAILURE]) {
      expectSanitised(friendlyScanError(raw).message)
    }
  })

  // The rules used to match bare substrings, so a message that merely NAMED a
  // column like these got a confident, wrong diagnosis (DATA-20).
  it('does not diagnose a timeout or a connect failure from a column name', () => {
    for (const raw of [
      'Column session_timeout has an unsupported type',
      'Unknown identifier connection_id in the base query',
      'Column refused_count is ambiguous',
    ]) {
      const result = friendlyScanError(raw)
      expect(result.message).toBe('Scan failed.')
      expect(result.technical).toBe(raw)
    }
  })

  it('still reads the drivers\' own exception names', () => {
    expect(friendlyScanError('requests.exceptions.ReadTimeout: ...').message).toBe(
      'Scan failed: the data source did not respond in time.',
    )
    expect(friendlyScanError('ConnectionError: reset by peer').message).toBe(
      'Scan failed: could not connect to the data source.',
    )
  })
})
