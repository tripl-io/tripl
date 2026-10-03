import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { WELCOME_PILLARS } from '@/components/workspace-welcome-pillars'
import { DEMO_PROVISION_ESTIMATE } from '@/demo/provisioningPhases'
import { ExternalLink, Plus, Sparkles } from 'lucide-react'

interface WorkspaceWelcomeProps {
  canCreateProject: boolean
  isProvisioningDemo: boolean
  onGenerateDemo: () => void
  /** Absent where blank projects are not offered (a public demo). */
  onCreateProject?: () => void
}

/**
 * Post-registration welcome hero for the empty workspace (tripl-odrj.1):
 * explains what tripl is and offers the two ways in — a generated demo or an
 * empty project. Replaces the all-zero stat band until the first project exists.
 */
export function WorkspaceWelcome({
  canCreateProject,
  isProvisioningDemo,
  onGenerateDemo,
  onCreateProject,
}: WorkspaceWelcomeProps) {
  return (
    <section className="space-y-8 py-4">
      <div className="mx-auto flex max-w-2xl flex-col items-center space-y-3 text-center">
        <Chip tone="accent" size="sm">
          Tracking plan operations
        </Chip>
        <h2 className="m-0 text-display font-semibold leading-tight tracking-[-0.02em]">
          Keep your product analytics honest
        </h2>
        <p className="m-0 text-body leading-relaxed text-fg-secondary">
          tripl is the single place where your team writes down what you <em>intend</em> to track,
          checks it against what your apps are <em>actually</em> sending, and gets a heads-up the
          moment the numbers start to look wrong.
        </p>
        <p className="m-0 text-body-sm text-fg-tertiary">
          No new SDK to ship and nothing to re-instrument — tripl connects to the data warehouse
          you already have (ClickHouse, BigQuery, or PostgreSQL) and only ever reads from it.
        </p>
      </div>

      {canCreateProject ? (
        <div className="mx-auto flex max-w-2xl flex-col justify-center gap-x-8 gap-y-4 sm:flex-row sm:items-start">
          <div className="flex flex-col gap-1.5 sm:items-center sm:text-center">
            {/* Empty-state CTAs take the large control size (DS-14). */}
            <Button size="lg" onClick={onGenerateDemo} disabled={isProvisioningDemo}>
              <Sparkles className="size-3.5" aria-hidden="true" />
              {isProvisioningDemo ? 'Generating…' : 'Generate demo project'}
            </Button>
            <p className="m-0 max-w-[280px] text-caption text-fg-tertiary">
              Builds a complete example in {DEMO_PROVISION_ESTIMATE} — local synthetic data, real
              scans and alert rules. Reset or delete it any time.
            </p>
          </div>
          {onCreateProject && (
            <div className="flex flex-col gap-1.5 sm:items-center sm:text-center">
              <Button size="lg" variant="outline" onClick={onCreateProject}>
                <Plus className="h-3.5 w-3.5" />
                New project
              </Button>
              <p className="m-0 max-w-[280px] text-caption text-fg-tertiary">
                Start empty or from an industry template, then connect your own warehouse.
              </p>
            </div>
          )}
        </div>
      ) : (
        <p
          className="mx-auto max-w-md text-center text-body-sm text-fg-tertiary"
        >
          Ask a workspace owner or editor to create the first project — you&apos;ll see it here as
          soon as it exists.
        </p>
      )}

      <div className="grid gap-3 lg:grid-cols-3">
        {WELCOME_PILLARS.map((pillar) => (
          <Card key={pillar.id}>
            <CardContent className="space-y-2">
              <div className="flex items-center gap-2.5">
                <div
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent"
                >
                  <pillar.icon className="h-4 w-4" />
                </div>
                <p
                  className="m-0 micro-label text-fg-tertiary"
                >
                  {pillar.eyebrow}
                </p>
              </div>
              <h3 className="m-0 text-body font-semibold tracking-tight">{pillar.title}</h3>
              <p className="m-0 text-body-sm leading-[1.5] text-fg-secondary">
                {pillar.description}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      <div
        className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-card border px-4 py-3 text-center text-body-sm bg-surface border-border text-fg-secondary"
      >
        <span>
          Scan the warehouse → collect metrics → watch the charts — the same loop your real
          project runs on a schedule.
        </span>
        <a
          href="https://vladenisov.github.io/tripl/"
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 font-medium hover:underline text-accent"
          // It leaves the app, so it says so — visibly with the icon, and to a
          // screen reader in a name that starts with the visible label (WS-46).
          aria-label="Read the concepts (opens in a new tab)"
        >
          Read the concepts
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </a>
      </div>
    </section>
  )
}
