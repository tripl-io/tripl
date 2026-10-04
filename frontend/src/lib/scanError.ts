/**
 * Scan-error sanitiser (issue H3).
 *
 * Raw backend exceptions must never surface verbatim in the UI: a ClickHouse
 * `HTTPSConnectionPool` read-timeout leaks host/port, a SQLAlchemy
 * `autoflush`/`no_autoflush` hint leaks the ORM internals, and a socket failure
 * leaks `getaddrinfo`/`host=`/`port=`. `friendlyScanError` maps a known raw
 * error to a safe, user-facing `message` and preserves the original string as
 * `technical` so an owner-only "View technical details" expander can show it.
 *
 * Surfaces: dashboard latest-scan card, overview Recent-activity feed, Scans page.
 */

export interface FriendlyScanError {
  /** Safe, user-facing message. Never contains a host, port, library, ORM hint, or function name. */
  message: string
  /** Original raw error, surfaced only behind an owner-only expander. Absent when `raw` is empty. */
  technical?: string
}

const SCAN_FAILED = 'Scan failed.'
const SCAN_FAILED_TIMEOUT = 'Scan failed: the data source did not respond in time.'
const SCAN_FAILED_INTERNAL = 'Scan failed: an internal error occurred while saving results.'
const SCAN_FAILED_CONNECT = 'Scan failed: could not connect to the data source.'

/**
 * Ordered rules — first match wins, tested against the lowercased text. Timeout
 * is checked before connection because a `HTTPSConnectionPool(host=..., port=...):
 * Read timed out` string also contains "Connection"/`host=`/`port=` and must
 * read as a timeout, not a generic connect failure.
 *
 * Word-like patterns are anchored to token boundaries, the same way
 * RAW_INTERNAL_MARKERS below are and for the same reason: as bare substrings,
 * any message naming a column such as `session_timeout` or `connection_id` read
 * as "the data source did not respond in time" or "could not connect" — a
 * confidently wrong diagnosis rather than the generic one (DATA-20). The
 * driver's own spellings (`ReadTimeout`, `ConnectionError`, `ConnectionRefused`)
 * are listed whole, since a boundary check would otherwise reject them.
 */
const BOUNDARY_BEFORE = '(?<![a-z0-9_])'
const BOUNDARY_AFTER = '(?![a-z0-9_])'
const token = (pattern: string) => new RegExp(`${BOUNDARY_BEFORE}(?:${pattern})${BOUNDARY_AFTER}`)

const SCAN_ERROR_RULES: ReadonlyArray<{ patterns: readonly RegExp[]; message: string }> = [
  {
    patterns: [
      token('httpsconnectionpool'),
      token('(?:read|connect)?timeout(?:error|exception)?'),
      token('timed out'),
    ],
    message: SCAN_FAILED_TIMEOUT,
  },
  {
    patterns: [token('(?:no_)?autoflush'), token('sqlalchemy')],
    message: SCAN_FAILED_INTERNAL,
  },
  {
    patterns: [
      token('connection(?:error|refused(?:error)?|reset(?:error)?)?'),
      token('refused'),
      token('getaddrinfo'),
      /host=/,
      /port=/,
    ],
    message: SCAN_FAILED_CONNECT,
  },
]

/**
 * Markers that betray a raw, unsanitised exception (host/port, driver, ORM,
 * socket, stack frames). The backend now sanitises scan failures into specific
 * "Scan failed: …" messages; when text reads as one of those AND carries none
 * of these markers, it is safe to show verbatim. This guard makes the
 * pass-through below leak-proof even if some path prefixes raw text with
 * "Scan failed:".
 *
 * Anchored to token boundaries, not matched as bare substrings. One of those
 * curated messages enumerates the warehouse's own column names ("Available keys:
 * …"), so `client_errno` or `error_traceback_id` used to read as a raw exception
 * — the pass-through was then skipped, the text fell through to SCAN_ERROR_RULES
 * below, and a sibling column called `connection_id` mapped it to an actively
 * WRONG "could not connect to the data source". The `host=` / `port=`
 * / ` object at 0x` markers carry characters no SQL identifier has, so they stay
 * plain substrings. A column named EXACTLY `errno` still trips the guard: that is
 * the leak-proof side of the bargain, and it costs one generic message, never a
 * wrong one.
 */
const RAW_INTERNAL_MARKERS: readonly RegExp[] = [
  /(?<![a-z0-9_])httpsconnectionpool(?![a-z0-9_])/,
  /(?<![a-z0-9_])sqlalchemy(?![a-z0-9_])/,
  // `no_autoflush` is the SQLAlchemy hint's own spelling, so it is part of the
  // marker rather than something a left boundary should exclude.
  /(?<![a-z0-9_])(?:no_)?autoflush(?![a-z0-9_])/,
  /(?<![a-z0-9_])getaddrinfo(?![a-z0-9_])/,
  /(?<![a-z0-9_])errno(?![a-z0-9_])/,
  /(?<![a-z0-9_])traceback(?![a-z0-9_])/,
  /host=/,
  /port=/,
  / object at 0x/,
]

export function friendlyScanError(raw: string | null | undefined): FriendlyScanError {
  if (!raw || raw.trim() === '') {
    return { message: SCAN_FAILED }
  }

  const trimmed = raw.trim()
  const haystack = trimmed.toLowerCase()

  // The backend already turns scan failures into specific, safe "Scan failed: …"
  // messages. When the text reads as one of those and carries no raw internals,
  // show it verbatim so users keep the specific reason instead of the generic
  // fallback. Pre-sanitisation records and genuinely raw strings fall through to
  // the mapping rules below.
  //
  // The prefix is an invariant of the sender rather than a hope about it:
  // `user_facing_error` (backend/src/tripl/worker/tasks/_errors.py) prefixes
  // every curated message it returns, so this test asks "did this come from that
  // function", not "does this happen to be worded a certain way". Until
  // tripl-7bol it WAS a hope, and it was wrong for every curated message in the
  // backend: all ~60 arrived without the prefix and were collapsed right here
  // into the generic line they had been written to replace.
  const hasRawInternals = RAW_INTERNAL_MARKERS.some((marker) => marker.test(haystack))
  if (!hasRawInternals && haystack.startsWith('scan failed')) {
    return { message: trimmed }
  }

  for (const rule of SCAN_ERROR_RULES) {
    if (rule.patterns.some((pattern) => pattern.test(haystack))) {
      return { message: rule.message, technical: raw }
    }
  }

  return { message: SCAN_FAILED, technical: raw }
}
