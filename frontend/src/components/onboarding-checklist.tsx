import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Check, ChevronDown, ChevronUp, Lock, X } from 'lucide-react'
import { toast } from 'sonner'
import { Chip } from '@/components/primitives/chip'
import { Panel } from '@/components/settings/kit'
import {
  buildOnboardingSteps,
  onboardingProgress,
  type OnboardingStep,
  type OnboardingStepId,
} from '@/components/onboarding-steps'
import { useAuth } from '@/components/auth-context'
import type { ProjectSummary } from '@/types'
import { isOwner as isOwnerRole, useCanWriteProject } from '@/lib/permissions'
import { isOnboardingDismissed, setOnboardingDismissed } from '@/lib/onboardingDismissal'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * Guided first-run checklist (UX-24). A newcomer lands on the Overview with no
 * "start here"; this surfaces the core loop as up to five concrete steps, in
 * the order of the fastest path: connect, scan, review, metric, alert (JR-2;
 * the steps themselves live in onboarding-steps.ts). Each step's done-state is
 * derived from REAL project state (the cheap project summary + the
 * data-sources count already loaded by the Overview), so steps tick off automatically as the user makes progress —
 * nothing is stored as a manual "I did this" flag.
 *
 * It is deliberately compact and self-effacing: the X is a *persistent*
 * dismiss, remembered per project in localStorage (lib/onboardingDismissal.ts),
 * so once dismissed it stays dismissed across remounts and later visits. It is
 * not a one-way door: the dismissal offers Undo, and the command palette's
 * "Show getting started" brings it back (WS-35). The card also
 * auto-hides on its own in two cases so it never becomes permanent chrome:
 * once every step is complete, and — crucially for a mature
 * project — once the core loop is set up and coverage is high but the only
 * remaining step is an optional one the owner deliberately skipped (e.g.
 * alerting). Genuinely new / low-coverage projects still see it. When the user
 * is all-but-done (every step bar the last), it collapses to a slim one-line bar
 * naming the single remaining step, which can be expanded on demand, so a
 * nearly-onboarded project isn't dominated by a tall card (fix #13).
 *
 * The checklist is role-aware. Some steps are actionable only by
 * an owner — connecting a data source is owner-only — and a self-registered /
 * invited user is an editor, not an owner. For a non-owner an owner-only step
 * would otherwise be a silent dead-end (they see the list with no "Add
 * connection" button and can never tick it off), so it is shown but marked
 * "Owner only" with an ask-an-owner hint AND excluded from progress: it never
 * blocks completion, letting an editor's checklist actually reach done.
 */

// Steps a mature owner can legitimately leave undone forever. Alerting is opt-in:
// a high-coverage project that never wires up a destination should not be nagged
// by a checklist that, by design, can never reach 5 of 5. A key
// metric is the same for a project that predates the metric step (JR-2).
const OPTIONAL_STEP_IDS: ReadonlySet<OnboardingStepId> = new Set<OnboardingStepId>(['alert', 'metric'])

// Reconciliation coverage (implemented ÷ active planned events) at/above this
// ratio marks a project as genuinely established rather than mid-onboarding.
const MATURE_COVERAGE_RATIO = 0.8

type StepState = 'done' | 'active' | 'upcoming' | 'owner-only'

interface OnboardingChecklistProps {
  slug: string
  /** Keys the dismissal, so it survives a slug rename (WS-35). */
  projectId?: string
  summary: ProjectSummary | undefined
  /** Number of connected data sources (the Overview already lists these). */
  sourceCount: number
  /**
   * Number of metrics in the project, for the "Define a key metric" step. The
   * project summary does not carry it yet; with neither this nor a summary
   * count the step is left out rather than shown un-tickable.
   */
  metricCount?: number
  /**
   * Demo projects own their own onboarding (DemoWelcomePanel + coach), and a
   * demo's only source is synthetic — excluded from `sourceCount` — so the
   * "Connect a data source" step could never complete and the checklist would
   * be stuck at "4 of 5" forever. Hide the checklist entirely for demos.
   */
  isDemo?: boolean
}

/** Reconciliation coverage: share of active planned events actually arriving. */
function coverageRatio(summary: ProjectSummary): number {
  if (summary.active_event_count <= 0) return 0
  return summary.implemented_event_count / summary.active_event_count
}

export function OnboardingChecklist({
  slug,
  projectId,
  summary,
  sourceCount,
  metricCount,
  isDemo,
}: OnboardingChecklistProps) {
  const { user } = useAuth()
  const canWriteHere = useCanWriteProject()
  const stepsId = useId()
  // A tick to force a re-render (and thus a re-read of localStorage) after
  // dismissal. Reading dismissal on render also means a slug change is picked up
  // automatically, with no stale per-project state.
  const [, setDismissTick] = useState(0)
  // Ephemeral: when the slim collapsed bar is expanded back to the full card.
  const [expanded, setExpanded] = useState(false)

  // Demo projects have their own onboarding (DemoWelcomePanel + coach). Their
  // only source is synthetic, so "Connect a data source" can never complete and
  // the checklist would be stuck at "4 of 5" forever — hide it.
  if (isDemo) return null

  // Not loaded yet — render nothing rather than a checklist full of false
  // "incomplete" steps that would flip to done a moment later.
  if (!summary) return null

  if (isOnboardingDismissed(slug, projectId)) return null

  // Every step is an editor's job (scans, review, metrics and alerting are
  // editor-gated, sources owner-only). For a viewer of this project the card was
  // a to-do list they could never work through and never finish, pinned until
  // dismissed — so it is simply not theirs.
  if (!canWriteHere) return null

  const isOwner = isOwnerRole(user?.role)
  const steps = buildOnboardingSteps(slug, summary, sourceCount, metricCount)
  // Owner-only steps don't count toward a non-owner's progress: an editor can't
  // action them, so counting them would leave the checklist permanently short of
  // "done" with no way forward. They stay in `steps` (still shown,
  // still discoverable) but drop out of the progress/self-hide math below.
  const counts = (s: OnboardingStep): boolean => isOwner || !s.ownerOnly
  const remainingSteps = steps.filter((s) => counts(s) && !s.done)
  const { completed, total } = onboardingProgress(steps, isOwner)

  // Self-hiding, two ways:
  //   1. Every step is complete — nothing left to guide.
  //   2. Established project: the core loop is set up (every remaining step is
  //      optional, i.e. one the owner can legitimately skip) AND coverage is
  //      high. Without this, a mature project that deliberately skipped an
  //      optional step (e.g. alerting) would show "4 of 5" as permanent chrome.
  // Genuinely new / low-coverage projects fall through and still see the card.
  const onlyOptionalRemains =
    remainingSteps.length > 0 && remainingSteps.every((s) => OPTIONAL_STEP_IDS.has(s.id))
  const isEstablished = onlyOptionalRemains && coverageRatio(summary) >= MATURE_COVERAGE_RATIO
  if (completed >= total || isEstablished) return null

  function setDismissed(dismissed: boolean): void {
    setOnboardingDismissed(slug, projectId, dismissed)
    setDismissTick((n) => n + 1)
  }

  function handleDismiss(): void {
    setDismissed(true)
    toast('Getting started hidden', {
      id: `onboarding-dismissed:${projectId ?? slug}`,
      description: 'Bring it back any time with "Show getting started" in search.',
      action: { label: 'Undo', onClick: () => setDismissed(false) },
    })
  }

  // Mostly onboarded (every step bar the last) → swap the tall card for a slim
  // one-line bar with progress + an expand affordance, instead of letting a
  // near-complete checklist hog the top of the Overview. Early-stage projects
  // (more than one step left) keep the full card. Dismiss + localStorage
  // persistence are unchanged (fix #13).
  const isMostlyDone = completed >= total - 1
  // Here exactly one step is outstanding (mostly-done but not complete). Name
  // it inline so the bar says *which* step is left, not just the count,
  // while keeping the "1 step left" detail.
  const nextStep = remainingSteps[0]
  if (isMostlyDone && !expanded && nextStep) {
    return (
      <div
        className="flex items-center gap-3 rounded-lg border px-4 py-2.5 bg-bg-sunken border-border-subtle"
      >
        <Chip tone="info" size="sm">{`${completed} of ${total}`}</Chip>
        <span className="min-w-0 flex-1 truncate text-body-sm font-medium">
          {`Almost set up — 1 step left: ${nextStep.title}`}
        </span>
        <button
          type="button"
          onClick={() => setExpanded(true)}
          aria-expanded={expanded}
          aria-controls={stepsId}
          className="flex shrink-0 items-center gap-1 rounded-sm px-2 py-1 text-caption font-medium transition-colors hover:bg-[var(--surface-hover)] text-accent"
        >
          Show steps
          <ChevronDown className="h-3 w-3" />
        </button>
        <button
          type="button"
          onClick={handleDismiss}
          aria-label="Dismiss getting-started checklist"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-tertiary"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    )
  }

  // The first not-yet-done step the current user can actually action is the
  // "active" one; later incomplete steps are upcoming. They are links like any
  // other step, so they keep full opacity and only their number goes muted: at
  // 60% they read as disabled while still being clickable (SH-37). An
  // owner-only step is skipped over for a non-owner so "Next" never lands on a
  // step they can't complete.
  const activeIndex = steps.findIndex((s) => !s.done && counts(s))

  return (
    <Panel
      title="Get started"
      subtitle={
        <>
          {`${steps.length} steps to your first monitored event`} ·{' '}
          {/* The glossary, for a reader who does not know the words the
              steps use yet (#238 JR-32). */}
          <Link to={projectPath(currentOrgSlug(), slug, '/concepts')} className="text-accent no-underline hover:underline">
            What is this?
          </Link>
        </>
      }
      right={
        <div className="flex items-center gap-2">
          <Chip tone="info" size="sm">{`${completed} of ${total}`}</Chip>
          {/* Expanded from the slim bar: the way back to it (WS-35). */}
          {isMostlyDone && (
            <button
              type="button"
              onClick={() => setExpanded(false)}
              aria-expanded={expanded}
              aria-controls={stepsId}
              className="flex items-center gap-1 rounded-sm px-2 py-1 text-caption font-medium transition-colors hover:bg-[var(--surface-hover)] text-accent"
            >
              Hide steps
              <ChevronUp className="h-3 w-3" aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            onClick={handleDismiss}
            aria-label="Dismiss getting-started checklist"
            className="flex h-6 w-6 items-center justify-center rounded-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-tertiary"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      }
    >
      <ol id={stepsId} aria-label="Setup steps" className="divide-y border-border-subtle">
        {steps.map((step, index) => (
          <StepRow
            key={step.id}
            step={step}
            number={index + 1}
            state={stepState(step, index, activeIndex, isOwner)}
          />
        ))}
      </ol>
    </Panel>
  )
}

/** Visual state for a step, accounting for the current user's role. */
function stepState(
  step: OnboardingStep,
  index: number,
  activeIndex: number,
  isOwner: boolean,
): StepState {
  if (step.done) return 'done'
  if (step.ownerOnly && !isOwner) return 'owner-only'
  return index === activeIndex ? 'active' : 'upcoming'
}

function StepRow({ step, number, state }: { step: OnboardingStep; number: number; state: StepState }) {
  // On an owner-only step a non-owner sees the ask-an-owner hint, not the
  // do-it-yourself one — the action isn't theirs to take.
  const hint = state === 'owner-only' ? step.ownerHint ?? step.hint : step.hint
  return (
    <li>
      <Link
        to={step.href}
        aria-current={state === 'active' ? 'step' : undefined}
        className="flex items-center gap-3 px-4 py-2.5 no-underline transition-colors hover:bg-[var(--surface-hover)] text-inherit"
      >
        <StepIndicator state={state} number={number} />
        <div className="min-w-0 flex-1">
          {/* Wrapped to two lines on a phone rather than cut to a few words:
              the hint is the only guidance the step gives (WS-36). */}
          <div
            className="line-clamp-2 text-body-sm font-medium sm:truncate"
            style={{ color: state === 'done' ? 'var(--fg-subtle)' : 'var(--fg)' }}
          >
            {step.title}
          </div>
          <div
            className="line-clamp-2 text-caption sm:truncate text-fg-tertiary"
            title={hint}
          >
            {hint}
          </div>
        </div>
        {state === 'done' ? (
          <Chip tone="success" size="xs">
            Done
          </Chip>
        ) : state === 'active' ? (
          <Chip tone="accent" size="xs" icon={<ArrowRight className="h-3 w-3" />}>
            Next
          </Chip>
        ) : state === 'owner-only' ? (
          <Chip tone="neutral" size="xs" icon={<Lock className="h-3 w-3" />}>
            Owner only
          </Chip>
        ) : (
          <ArrowRight aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" />
        )}
      </Link>
      {/* A second way through the step, outside the row link so the two
          anchors do not nest (JR-2: no warehouse yet → add events by hand). */}
      {step.alternative && state !== 'done' && (
        <p className="m-0 -mt-1.5 pb-2.5 pl-[50px] pr-4 text-caption">
          <Link to={step.alternative.href} className="text-accent no-underline hover:underline">
            {step.alternative.label}
          </Link>
        </p>
      )}
    </li>
  )
}

function StepIndicator({ state, number }: { state: StepState; number: number }) {
  if (state === 'done') {
    return (
      <span
        aria-hidden="true"
        className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full bg-success-soft text-success"
      >
        <Check className="h-3.5 w-3.5" />
      </span>
    )
  }
  if (state === 'owner-only') {
    return (
      <span
        aria-hidden="true"
        className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full"
        style={{ border: '1px solid var(--border)', background: 'var(--surface-hover)', color: 'var(--fg-faint)' }}
      >
        <Lock className="h-3 w-3" />
      </span>
    )
  }
  // Active and upcoming steps carry their number; only the active one is in
  // the accent colour, so "what next" stands out without greying the rest.
  const isActive = state === 'active'
  return (
    <span
      aria-hidden="true"
      className="tnum flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full text-caption font-semibold"
      style={{
        border: `1px solid ${isActive ? 'var(--accent)' : 'var(--border)'}`,
        background: isActive ? 'var(--accent-soft)' : 'var(--bg-sunken)',
        color: isActive ? 'var(--accent)' : 'var(--fg-faint)',
      }}
    >
      {number}
    </span>
  )
}
