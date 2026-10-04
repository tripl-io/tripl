/**
 * What to do about a failed run, keyed off the friendly message
 * `friendlyScanError` already produced. The diagnosis used to end the story:
 * a timeout said the source "did not respond in time" and offered none of the
 * three things that fix one (#247).
 */
export interface ScanErrorNextStep {
  /** One sentence naming the fix. */
  text: string
  /** Where each fix lives. `to` is a router path; `?tab=…` stays on this scan. */
  actions: { label: string; to: string }[]
}

/**
 * The anchor the Limits section answers to: the configuration tab alone lands
 * on a collapsed section, so the link names the section it means.
 */
export const LIMITS_SECTION_ID = 'scan-limits'
const LIMITS_HREF = `?tab=configuration#${LIMITS_SECTION_ID}`

export function scanErrorNextStep(
  friendlyMessage: string,
  dataSourceId: string | null | undefined,
  /**
   * Data-source pages are owner-only: anyone else following the link is
   * bounced back to the list, so the connection action is not offered.
   */
  canOpenDataSource: boolean,
): ScanErrorNextStep | null {
  const message = friendlyMessage.toLowerCase()
  // "Open", not "Test": the route opens the connection's settings, where the
  // test lives — it does not run one on arrival.
  const testConnection = dataSourceId && canOpenDataSource
    ? [{ label: 'Open the connection', to: `/settings/data-sources/${dataSourceId}` }]
    : []
  if (message.includes('did not respond in time')) {
    return {
      text: 'Read less per run (a shorter Lookback or a lower row cap in Limits), or check that the warehouse is reachable and not overloaded.',
      actions: [{ label: 'Open Limits', to: LIMITS_HREF }, ...testConnection],
    }
  }
  if (message.includes('could not connect')) {
    return {
      text: 'Check the data source’s host, credentials and network access, then run the scan again.',
      actions: testConnection,
    }
  }
  if (message.includes('internal error')) {
    return {
      text: 'This failed on tripl’s side, not in your warehouse. Run the scan again; if it keeps failing, share the technical details with whoever runs tripl.',
      actions: [],
    }
  }
  return null
}
