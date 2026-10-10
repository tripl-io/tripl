import { ExternalLink } from 'lucide-react'
import { EDITIONS_DOCS_URL, type EnterpriseTeaser } from '@/extensions/teasers'

const LINK_CLASS =
  'inline-flex items-center gap-1 text-body-sm font-medium text-accent no-underline hover:underline'

/**
 * A settings section of the Enterprise edition, opened in Community: what the
 * feature does, that this edition does not have it, what Community has of the
 * same kind where it has something, and where to read more. The page title
 * above it is the section's own.
 */
export function EnterpriseFeature({ teaser }: { teaser: EnterpriseTeaser }) {
  // The feature's own rail icon: the Sparkles this badge used to carry is the
  // AI items' icon, and read as "an AI feature".
  const Icon = teaser.item.icon
  return (
    <section
      aria-labelledby="enterprise-feature-title"
      className="max-w-prose rounded-card border border-border bg-bg-elevated px-5 py-5"
    >
      <div className="flex items-start gap-3">
        <span className="rounded-full border border-accent/30 bg-accent-soft p-2 text-accent">
          <Icon className="size-4" aria-hidden="true" />
        </span>
        <div className="min-w-0 space-y-2">
          {/* "<label>: part of …", not "<label> is part of …": several labels
              are plural ("Organizations", "User accounts"). */}
          <h2 id="enterprise-feature-title" className="text-body font-semibold text-fg">
            {teaser.item.label}: part of tripl Enterprise
          </h2>
          <p className="text-body text-fg-secondary">{teaser.summary}</p>
          <p className="text-body text-fg-secondary">
            This instance runs the Community edition, which does not include it.
          </p>
          {teaser.inCommunity && (
            <p className="text-body text-fg-secondary">
              {teaser.inCommunity.text}{' '}
              <a href={teaser.inCommunity.href} target="_blank" rel="noreferrer" className={LINK_CLASS}>
                How to set it up
                <ExternalLink className="size-3.5" aria-hidden="true" />
              </a>
            </p>
          )}
          <a href={EDITIONS_DOCS_URL} target="_blank" rel="noreferrer" className={LINK_CLASS}>
            Compare editions
            <ExternalLink className="size-3.5" aria-hidden="true" />
          </a>
        </div>
      </div>
    </section>
  )
}
