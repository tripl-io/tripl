/**
 * The terms pages hint at, spelled as the Concepts glossary spells them and
 * cut to one line from its definitions. Spread one into a TermHint:
 * `titleAddon={slug && <TermHint slug={slug} {...TERM_HINTS.coverage} />}`.
 */
export const TERM_HINTS = {
  scans: {
    term: 'Scans',
    definition:
      'Warehouse queries that add events and fields to your plan; monitoring scans also record the metric points alerts are built on.',
  },
  factTables: {
    term: 'Fact tables',
    definition: 'A saved SQL query over your warehouse, one row per fact, that metrics are defined on.',
  },
  reconciliation: {
    term: 'Reconciliation',
    definition: 'Comparing your plan against the events that actually arrive, and resolving the differences.',
  },
  coverage: {
    term: 'Coverage',
    definition: 'The share of active planned events marked implemented.',
  },
  scopes: {
    term: 'Scopes',
    definition: 'The level activity and anomalies are measured at: the whole project, an event type, or one event.',
  },
} as const satisfies Record<string, { term: string; definition: string }>
