import { ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Chip } from '@/components/primitives/chip'
import { SCard, SHeader } from '@/components/settings/kit'
import { extensionSettingsSection } from '@/extensions'
import { EDITIONS_DOCS_URL } from '@/extensions/teasers'
import { currentOrgSlug, projectPath, settingsPath } from '@/lib/navigation'

/** Where an installed extension keeps the organization's plan rules. */
export const GOVERNANCE_SECTION_PATH = 'organization/governance'

const linkClass = 'font-medium underline underline-offset-2 text-accent'

/**
 * Project · Plan rules: the gates a plan change passes before it reaches main.
 *
 * Everything listed under "In this project" runs today and is configured
 * elsewhere (the merge policy on Plan branches, owners on each event type,
 * required fields and contracts on each field). Rules set once for the whole
 * organization are an extension's page; without one, this page says which
 * edition has them rather than offering controls that do nothing.
 */
export default function PlanRulesSection({ slug }: { slug?: string } = {}) {
  const governance = extensionSettingsSection(GOVERNANCE_SECTION_PATH)
  return (
    <div>
      <SHeader
        title="Plan rules"
        description="What a change to the tracking plan must pass before it reaches main."
      />

      <SCard title="In this project" description="These gates run on every merge of a plan branch.">
        <ul className="m-0 list-disc space-y-1.5 px-4 py-[15px] pl-9 text-body-sm leading-[1.5] text-fg-secondary">
          <li>
            Approvals: how many reviewers must approve a branch, and whether its author may approve
            it. Set in{' '}
            {slug ? (
              <Link to={projectPath(currentOrgSlug(), slug, '/branches')} className={linkClass}>
                Plan branches › Merge policy
              </Link>
            ) : (
              <span className="font-medium">Plan branches › Merge policy</span>
            )}
            .
          </li>
          <li>
            Owners: a branch that changes an owned event type needs one of its owners’ approval.
          </li>
          <li>
            Required fields and contracts: <code>tripl check</code> reports a tracking call that
            misses a required field or sends a value its contract does not allow.
          </li>
        </ul>
      </SCard>

      <div className="mt-4">
        <SCard
          title="Across the organization"
          description="Rules set once for every project: naming patterns, required and forbidden properties, approval of sensitive fields, and a protected main."
        >
          <div className="space-y-2 px-4 py-[15px] text-body-sm leading-[1.5] text-fg-secondary">
            {governance ? (
              <p className="m-0">
                They apply to this project too, and are set in{' '}
                <Link to={settingsPath(`/settings/${GOVERNANCE_SECTION_PATH}`)} className={linkClass}>
                  Organization › {governance.item.label}
                </Link>
                .
              </p>
            ) : (
              <>
                <p className="m-0 flex items-center gap-2">
                  <Chip tone="accent" size="sm">
                    Enterprise
                  </Chip>
                  <span>Organization rules are part of Tripl Enterprise.</span>
                </p>
                <p className="m-0">
                  This instance runs the Community edition, which keeps the per-project gates above.
                </p>
                <a
                  href={EDITIONS_DOCS_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 font-medium text-accent no-underline hover:underline"
                >
                  Compare editions
                  <ExternalLink className="size-3.5" aria-hidden="true" />
                </a>
              </>
            )}
          </div>
        </SCard>
      </div>
    </div>
  )
}
