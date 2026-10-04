import type { DestinationTestErrorKind } from '@/types'
import { describeCron } from './deliverySchedule'

/**
 * A destination's delivery schedule as a sentence, with what it is holding
 * folded in: "Weekdays at 09:00 UTC · 3 held for next digest".
 *
 * The card used to show "Custom schedule (0 9 * * 1-5) · UTC" and a separate
 * "nothing held" pill. {@link describeCron} names the cadence, the weekday
 * preset included.
 */
export function destinationScheduleLabel(
  cron: string,
  timezone: string | null | undefined,
  heldCount: number | null | undefined,
): string {
  const zone = timezone ? ` ${timezone}` : ''
  const held = heldCount ? ` · ${heldCount} held for next digest` : ''
  return `${describeCron(cron)}${zone}${held}`
}

/** A failed test send, in words, with the raw transport error kept for "Details". */
export interface TestFailureText {
  summary: string
  /** The error as the server sent it, when the summary replaced it; else null. */
  detail: string | null
}

// "HTTP Error 404" from urllib, "HTTP 403 from <host>" from the channel client.
const HTTP_STATUS = /HTTP(?: Error)? (\d{3})\b/i

/**
 * What a refused test send means, for someone who is not reading a Python
 * traceback.
 *
 * The server passes the channel library's exception text straight through:
 * "<urlopen error Tunnel connection failed: 403 Forbidden>", "HTTP Error 404:
 * Not Found". The common transport shapes get a sentence; the raw text stays
 * one click away. A message that matches none of them is already the channel's
 * own words ("Forbidden: bot was blocked by the user") and is shown as it is.
 */
export function describeTestFailure(
  raw: string | null | undefined,
  structured?: { error_kind?: DestinationTestErrorKind | null; http_status?: number | null },
): TestFailureText {
  const text = (raw ?? '').trim()
  if (!text) return { summary: 'no reason given', detail: null }
  const mapped = (summary: string): TestFailureText => ({ summary, detail: text })
  const httpSummary = (code: number): string => {
    if (code === 401 || code === 403) {
      return `The channel rejected the credentials (HTTP ${code}). Check the token or URL.`
    }
    if (code === 404) return 'The URL was not found (HTTP 404). Check the address.'
    if (code === 429) return 'The channel is rate-limiting requests (HTTP 429). Try again in a minute.'
    if (code >= 500) return `The channel had a server error (HTTP ${code}). Try again later.`
    return `The channel refused the request (HTTP ${code}).`
  }

  // The server's own classification first: it reads the exception
  // types, where the patterns below can only guess at their wording.
  switch (structured?.error_kind) {
    case 'http_status':
      if (typeof structured.http_status === 'number') return mapped(httpSummary(structured.http_status))
      break
    case 'dns':
      return mapped("Couldn't find that host. Check the URL.")
    case 'timeout':
      return mapped("The channel didn't answer in time.")
    case 'tls':
      return mapped("The channel's TLS certificate couldn't be verified.")
    case 'network':
      return mapped("Couldn't reach the URL (network or firewall blocked the request).")
    default:
      // config, policy, smtp and other already read as sentences: shown as sent.
      break
  }

  const status = HTTP_STATUS.exec(text)
  if (status) return mapped(httpSummary(Number(status[1])))
  if (/name or service not known|nodename nor servname|temporary failure in name resolution|getaddrinfo|could not be resolved/i.test(text)) {
    return mapped("Couldn't find that host. Check the URL.")
  }
  if (/timed out|timeout/i.test(text)) {
    return mapped("The channel didn't answer in time.")
  }
  if (/certificate|ssl/i.test(text)) {
    return mapped("The channel's TLS certificate couldn't be verified.")
  }
  if (/tunnel connection failed|proxy/i.test(text)) {
    return mapped("Couldn't reach the URL: a network proxy blocked the request.")
  }
  if (/connection refused|urlopen error|network is unreachable|connection reset/i.test(text)) {
    return mapped("Couldn't reach the URL (network or firewall blocked the request).")
  }
  return { summary: text, detail: null }
}
