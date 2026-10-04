import { Link, useParams } from 'react-router-dom'
import { ArrowRight, type LucideIcon } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHead, Panel } from '@/components/settings/kit'
import { PRODUCT_PILLARS, type PillarId } from '@/components/workspace-welcome-pillars'
import { termAnchor } from '@/lib/glossary'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * A single domain term: its plain-language definition and, where the concept has
 * a real home in the app, the route segment (relative to `/p/:slug`) it lives at.
 * `surface` names that page when it is not the term itself (a shadow event
 * appears on Reconciliation); the link always reads "Open <page>" (#238 DA-36).
 * `workspace` marks a path that is not under the project (`/settings/...`).
 */
type Term = {
  term: string
  definition: string
  path?: string
  surface?: string
  workspace?: boolean
}

// The anchor helper lives in lib so pages that link a term (TermHint) do not
// import this page. Re-exported for callers that already import it here.
// eslint-disable-next-line react-refresh/only-export-components
export { termAnchor } from '@/lib/glossary'

type AreaKey = PillarId

type Area = {
  key: AreaKey
  label: string
  tagline: string
  blurb: string
  icon: LucideIcon
  accent: string
  terms: readonly Term[]
}

/**
 * The three jobs tripl is built around. Plan → Observe → Govern is the spine of
 * the whole product: declare what *should* happen, watch what *actually* does,
 * then keep the two in sync. The glossary and the at-a-glance map below are both
 * driven from this one list, and each area's name, tagline and icon come from
 * PRODUCT_PILLARS, which the empty-workspace welcome hero reads too — so the
 * two places that introduce the pillars cannot describe them differently
 * (WS-46).
 */
const AREAS: readonly Area[] = [
  {
    key: 'plan',
    ...PRODUCT_PILLARS.plan,
    blurb: 'Declare the tracking plan: the events you expect, how they are shaped, and how they relate.',
    accent: 'var(--accent)',
    terms: [
      {
        term: 'Events',
        // No backticks: TermRow prints `definition` as a bare text node, so the
        // grave accents that once wrapped the example rendered as literal
        // characters on the finished reference page. The example
        // itself is the one the New event form models in its name placeholder
        // ("e.g. checkout:completed", EventForm.tsx) and the shape scan rules
        // generate from an `event_name_format` — the glossary used to teach a
        // third convention, leaving a first-time user with no idea which the
        // product expects.
        definition:
          'A single tracked action in your plan — checkout:completed, for example. Events are the atomic unit you instrument, observe, and govern.',
        path: '/events',
      },
      {
        term: 'Event types',
        definition:
          'Categories that group related events (for example lifecycle or commerce) so they can be organised and colour-coded together.',
        path: '/event-types',
      },
      {
        // Named as the sidebar names it (#238 AU-10). Not "Schema & fields":
        // an event type's own field definitions are its schema.
        term: 'Meta fields',
        definition:
          'Extra attributes every event carries whatever its type — owner team, Jira ticket, review date. The payload fields an event sends are defined per event type.',
        path: '/meta-fields',
      },
      {
        term: 'Properties',
        definition:
          'Reusable named values (thresholds, identifiers, constants) referenced across the plan so a value is defined once and used everywhere.',
        path: '/variables',
      },
      {
        term: 'Relations',
        definition:
          'Declared links between events — one event follows, depends on, or belongs with another — describing how the plan fits together.',
        path: '/relations',
      },
      {
        term: 'Plan branches',
        definition:
          'Isolated copies of the plan you can edit and review before merging, like version-control branches for your tracking plan.',
        path: '/branches',
      },
      {
        term: 'Plan history',
        definition:
          'The snapshots of the plan taken at each merge (and on demand), so you can see what the plan looked like at any point and what changed.',
        path: '/history',
      },
      {
        term: 'In review',
        definition:
          'An event status: the event is proposed and waiting for someone to confirm it. The review count on Overview and Events counts these. Not the same as a branch that is ready for review.',
        path: '/events',
        surface: 'Events',
      },
    ],
  },
  {
    key: 'observe',
    ...PRODUCT_PILLARS.observe,
    blurb: 'Watch real traffic against the plan: live volume, the scopes it flows through, and anything anomalous.',
    accent: 'var(--info)',
    terms: [
      {
        // The project's home, named as the sidebar names it (#238 SH-8).
        term: 'Overview',
        definition:
          "The project's home page: event volume, open signals, top events, source health and the getting-started checklist — the first place to see what your data is doing.",
        path: '/overview',
      },
      {
        term: 'Metrics',
        definition:
          'Numbers tripl computes from your warehouse on a schedule — a count, a sum or a ratio over a fact table — and watches for spikes and drops like event volume.',
        path: '/metrics',
      },
      {
        term: 'Metric points',
        definition:
          'The individual values a monitoring scan or a metric records, one per time bucket. Charts, anomaly detection and alerts are all built on them.',
      },
      {
        term: 'Fact tables',
        definition:
          'A saved SQL query over your warehouse that metrics are defined on: one row per fact (an order, a session), with a time column and the columns metrics sum or count.',
        path: '/metrics/fact-tables',
      },
      {
        term: 'Scopes',
        definition:
          'The level at which activity and anomalies are measured: the whole project, a single event type, or one event. Signals and alert rules are always scoped.',
      },
      {
        term: 'Signals',
        definition:
          'An open anomaly at a given scope — a spike or drop tripl found in the volume. Signals are raised automatically by detection on every scan; no alert rule has to exist for one to appear.',
        path: '/anomalies',
        surface: 'Anomalies',
      },
      {
        term: 'Anomalies',
        definition:
          'The page that lists every open signal in the project, newest first. It is the signal inbox — the sidebar badge beside it counts the same open signals.',
        path: '/anomalies',
      },
      {
        term: 'Detection settings',
        definition:
          'How sensitive anomaly detection is for this project: the thresholds and minimum volumes a spike or drop must pass before it becomes a signal.',
        path: '/settings/monitoring',
      },
      {
        // One name for the object (#238 JR-28). The glossary used to explain
        // that "monitor" and "alert rule" were two words for it.
        term: 'Alert rules',
        definition:
          'Rules layered on top of detection: an alert rule decides which signals matter for a scope and where they are sent, and carries its own live state — firing, warning or healthy. A project with no alert rules still raises signals — it just does not notify anyone about them.',
        path: '/alerting?section=monitors',
        surface: 'Alerting',
      },
      {
        term: 'Incidents',
        definition:
          'A group of signals an alert rule routed to your team, with a triage state: open, acknowledged, resolved or false positive. Signals are what detection found; incidents are the ones somebody owes an answer on.',
        path: '/alerting',
        surface: 'Alerting',
      },
      {
        term: 'Alerting',
        definition:
          'Everything that turns a signal into a notification somebody owes an answer on: the incident Inbox, the rules that route, the destinations (Slack, Telegram, webhooks, email, Jira, Linear) they route to, and the delivery log behind them.',
        path: '/alerting',
      },
    ],
  },
  {
    key: 'govern',
    ...PRODUCT_PILLARS.govern,
    blurb: 'Close the gap between plan and traffic: reconcile differences, scan for drift, and keep an audit trail.',
    accent: 'var(--success)',
    terms: [
      {
        term: 'Reconciliation',
        definition:
          'The workflow of comparing your plan against observed events and resolving the differences — adopting, ignoring, or archiving them.',
        path: '/reconciliation',
      },
      {
        term: 'Shadow events',
        definition:
          'Events arriving in your data that are not declared in the plan — observed but undeclared. They surface during reconciliation so you can adopt or ignore them.',
        path: '/reconciliation',
        surface: 'Reconciliation',
      },
      {
        term: 'Dead events',
        definition:
          'Events declared in the plan that have stopped arriving — planned but unobserved. They are candidates for archiving during reconciliation.',
        path: '/reconciliation',
        surface: 'Reconciliation',
      },
      {
        // The glossary is where someone who does not understand scans arrives on
        // purpose — often straight off a Telegram alert naming one. It has to
        // carry the same chain the scans list, the scan form and a scan's own
        // page carry, in the same words. The old definition named
        // plan coverage and dead events and nothing downstream, and called every
        // scan "scheduled" — which a catalog-only scan is not.
        term: 'Scans',
        definition:
          'Warehouse queries that add events and fields to your tracking plan. A monitoring scan also records metric points on a schedule, and those points are what anomaly detection and alerts are built on.',
        path: '/scans',
      },
      {
        term: 'Catalog and monitoring scans',
        definition:
          'A catalog scan only adds events and fields to the plan. A monitoring scan does that and also records metric points on a schedule, which is what anomaly detection reads.',
        path: '/scans',
        surface: 'Scans',
      },
      {
        term: 'Data sources',
        definition:
          'The warehouse connections (ClickHouse, Postgres, BigQuery…) scans and metrics query. They are shared across the workspace and set up in Settings.',
        path: '/settings/data-sources',
        workspace: true,
      },
      {
        term: 'Coverage',
        definition:
          'The share of active planned events marked implemented. Different from the data match on Reconciliation, which compares the plan with what actually arrives.',
        path: '/coverage',
      },
      {
        term: 'Audit log',
        definition:
          "A chronological record of who changed what in this project's plan, so every adoption, edit, and archive is traceable. Workspace-level changes (members, API keys) are in the instance audit log.",
        path: '/audit',
      },
    ],
  },
]

function MapCard({ area }: { area: Area }) {
  const Icon = area.icon
  return (
    <div
      className="flex flex-col rounded-card border p-4 bg-surface border-border"
    >
      <div className="flex items-center gap-2">
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface-hover"
        >
          <Icon className="size-4" style={{ color: area.accent }} aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <div className="text-body font-semibold leading-tight">{area.label}</div>
          <div className="text-caption text-fg-tertiary">
            {area.tagline}
          </div>
        </div>
      </div>
      <p className="mt-2.5 text-body-sm leading-relaxed text-fg-tertiary">
        {area.blurb}
      </p>
      {/* Each chip jumps to its glossary row: chips that looked like links
          and did nothing were a dead end (#238 DA-36). */}
      <div className="mt-3 flex flex-wrap gap-1.5">
        {area.terms.map((t) => (
          <a
            key={t.term}
            href={`#${termAnchor(t.term)}`}
            className="rounded-full no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          >
            <Chip size="xs" className="cursor-pointer hover:bg-surface-active">
              {t.term}
            </Chip>
          </a>
        ))}
      </div>
    </div>
  )
}

function TermRow({ term, slug }: { term: Term; slug: string | undefined }) {
  const href = term.path
    ? term.workspace
      ? term.path
      : slug
        ? projectPath(currentOrgSlug(), slug, `${term.path}`)
        : undefined
    : undefined
  // One label shape for every row, "Open <page>" (#238 DA-36): most rows said
  // "Open", some named a page, and the right column was ragged.
  const page = term.surface ?? term.term
  const label = `Open ${page}`
  return (
    <div id={termAnchor(term.term)} className="scroll-mt-4 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <h4 className="text-body font-semibold">{term.term}</h4>
        {href && (
          // The name STARTS with the visible label (WCAG 2.5.3, WS-45), and
          // says which term sent the reader there when the page is another
          // one's.
          <Link
            to={href}
            className="flex shrink-0 items-center gap-0.5 text-caption font-medium no-underline text-accent"
            aria-label={term.surface ? `${label}, for ${term.term}` : `${label} in the app`}
          >
            {label}
            <ArrowRight className="size-3" aria-hidden="true" />
          </Link>
        )}
      </div>
      <p className="mt-1 text-body-sm leading-relaxed text-fg-tertiary">
        {term.definition}
      </p>
    </div>
  )
}

export default function ConceptsPage() {
  const { slug } = useParams<{ slug: string }>()

  return (
    <PageContainer className="space-y-8">
      <PageHead
        eyebrow="Help & reference"
        title="Concepts"
        description="How tripl models your plan. Three jobs turn a tracking plan into something you can trust: plan what should happen, observe what actually does, and govern the gap between them."
      />

      {/* Concept map — the Plan → Observe → Govern spine, with each job's key
          entities listed at a glance. The glossary below defines every term. */}
      <section aria-labelledby="concept-map-heading" className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 id="concept-map-heading" className="text-body font-semibold">
            How tripl models your plan
          </h2>
          <div
            className="flex items-center gap-1.5 text-caption font-medium text-fg-tertiary"
          >
            {AREAS.map((area, i) => (
              <span key={area.key} className="flex items-center gap-1.5">
                {i > 0 && <ArrowRight className="size-3" aria-hidden="true" />}
                <span>{area.label}</span>
              </span>
            ))}
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-3">
          {AREAS.map((area) => (
            <MapCard key={area.key} area={area} />
          ))}
        </div>
      </section>

      {/* Glossary — grouped by the same three jobs so the vocabulary maps onto
          the model above. Each term links to where it lives, when it has a home. */}
      <section aria-labelledby="glossary-heading" className="space-y-4">
        <h2 id="glossary-heading" className="text-body font-semibold">
          Glossary
        </h2>
        {AREAS.map((area) => (
          // h2 Glossary → h3 area → h4 term: the panels sit under the page's own
          // h2, so they must not flatten the outline back to level 2.
          <Panel key={area.key} title={area.label} subtitle={area.tagline} headingLevel={3}>
            <div className="divide-y border-border-subtle">
              {area.terms.map((term) => (
                <TermRow key={term.term} term={term} slug={slug} />
              ))}
            </div>
          </Panel>
        ))}
      </section>
    </PageContainer>
  )
}
