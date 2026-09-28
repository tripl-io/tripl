import { Link } from 'react-router-dom'
import { Chip } from '@/components/primitives/chip'
import { SCard, SHeader } from '@/components/settings/kit'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * The guardrails this page will hold once they exist. Naming conventions,
 * governance and PII handling have no API behind them — no endpoint, no
 * column, no worker reads any of it.
 */
const PLANNED_RULES: ReadonlyArray<{ title: string; items: readonly string[] }> = [
  {
    title: 'Naming conventions',
    items: [
      'Reject event names that do not match a chosen case style (snake_case, camelCase or Title Case) or a pattern.',
    ],
  },
  {
    title: 'Governance',
    items: [
      // Not "require an approval": approving plan CHANGES already exists, as the
      // merge policy (PL-26). This is a gate on an event's own status.
      'Event status gates: require a sign-off before an event’s status moves to live.',
      'Require every event to name a responsible person.',
      'Flag events that have had no volume for a sustained window.',
    ],
  },
  {
    title: 'PII & compliance',
    items: [
      'Scan field values for likely personal data, and block it without a consent tag.',
      'Set how long raw events are kept before roll-up.',
    ],
  },
]

/**
 * Project · Plan rules: a description of a feature that is not built.
 *
 * It was first a page of live controls pre-set to a governed state, which an
 * owner could read as proof their plan was protected (tripl-x2ho), and then a
 * page of dozens of disabled switches, radios and selects set to "off" — honest,
 * but still a quarter of the settings rail spent on controls that do nothing
 * (WS-37). What is left is one card that says what the rules will cover, with
 * no control that could be mistaken for a setting.
 */
export default function PlanRulesSection({ slug }: { slug?: string } = {}) {
  return (
    <div>
      <SHeader
        title="Plan rules"
        description="Guardrails for the tracking plan. None of them run today: no event name is checked, and nothing is required before an event goes live."
        actions={<Chip tone="warning" size="md">Not built yet</Chip>}
      />

      {/* What exists today, so the page is not a dead end (PL-26). */}
      <p className="mb-4 text-body-sm text-fg-secondary">
        Approvals for plan changes already work: they are set in{' '}
        {slug ? (
          <Link
            to={projectPath(currentOrgSlug(), slug, '/branches')}
            className="font-medium underline underline-offset-2 text-accent"
          >
            Plan branches › Merge policy
          </Link>
        ) : (
          <span className="font-medium">Plan branches › Merge policy</span>
        )}
        .
      </p>

      <SCard
        title="Coming later"
        description="What these rules will cover once they are built. There is nothing to configure yet."
      >
        <div className="space-y-4 px-4 py-[15px]">
          {PLANNED_RULES.map((group, index) => (
            <section key={group.title} aria-labelledby={`plan-rules-${index}`}>
              <h3
                id={`plan-rules-${index}`}
                className="m-0 text-body font-medium"
              >
                {group.title}
              </h3>
              <ul
                className="m-0 mt-1 list-disc space-y-0.5 pl-5 text-body-sm leading-[1.5] text-fg-tertiary"
              >
                {group.items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </SCard>
    </div>
  )
}
