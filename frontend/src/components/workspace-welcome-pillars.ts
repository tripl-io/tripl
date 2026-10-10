import { Activity, ClipboardList, ShieldCheck } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export type PillarId = 'plan' | 'observe' | 'govern'

/**
 * The three jobs tripl is built around — Plan → Observe → Govern — named once.
 *
 * The welcome hero and the Concepts page each wrote their own description of
 * the same three pillars, and the two had already drifted apart: the hero said
 * anomaly detection needed "Nothing to configure" while Concepts explained the
 * scans and monitors that have to be set up. Both now take the names,
 * taglines and icons from here. Lives apart from any component so tests can
 * import it without tripping react-refresh's components-only rule.
 */
export const PRODUCT_PILLARS: Readonly<
  Record<PillarId, { label: string; tagline: string; icon: LucideIcon }>
> = {
  plan: { label: 'Plan', tagline: 'what should happen', icon: ClipboardList },
  observe: { label: 'Observe', tagline: 'what actually happens', icon: Activity },
  govern: { label: 'Govern', tagline: 'keep plan & reality in sync', icon: ShieldCheck },
}

/**
 * The product's one-line story, told the same way on the sign-in page and the
 * welcome screen.
 */
export const PRODUCT_STORY = {
  eyebrow: 'Tracking plan operations',
  headline: 'Keep your product analytics honest',
} as const

/** Product pillars shown on the empty-workspace welcome hero. */
export const WELCOME_PILLARS: ReadonlyArray<{
  id: PillarId
  icon: LucideIcon
  eyebrow: string
  title: string
  description: string
}> = [
  {
    id: 'plan',
    icon: PRODUCT_PILLARS.plan.icon,
    eyebrow: PRODUCT_PILLARS.plan.label,
    title: 'Design what should be tracked',
    description:
      'A searchable catalog of every event, its fields, and typed properties. Changes happen on branches that are reviewed and merged like pull requests, so the live plan is never broken by accident.',
  },
  {
    id: 'observe',
    icon: PRODUCT_PILLARS.observe.icon,
    eyebrow: PRODUCT_PILLARS.observe.label,
    title: 'Watch the real data',
    // Not "Nothing to configure": signals need a monitoring scan that records
    // metric points, and notifications need a monitor — the Concepts glossary
    // says so, and this card has to agree with it.
    description:
      "Scans read your warehouse, and once a monitoring scan records metrics, anomaly detection learns each event's normal rhythm — flagging spikes, drops, and schema drift. Alert rules decide who hears about it.",
  },
  {
    id: 'govern',
    icon: PRODUCT_PILLARS.govern.icon,
    eyebrow: PRODUCT_PILLARS.govern.label,
    title: 'Stay in control',
    description:
      'Reconciliation shows what is documented-but-dead and what arrives undocumented. Coverage, an audit log, roles, and scoped API keys keep the plan honest.',
  },
]
