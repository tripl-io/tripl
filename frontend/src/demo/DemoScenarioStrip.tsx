/**
 * The persistent scenario strip.
 *
 * Mounted inside the demo banner's row on every surface (one bar, not
 * two stacked blocks), so the active chapter's
 * step chain stays visible while the user walks the app. It renders nothing but
 * what the context already decided: the chapter, the step, the deep link,
 * whether a watch is in flight, and why live-loop went backwards. When a
 * chapter lands it offers the next one in order, beside Restart and a
 * per-chapter Dismiss — and when there is no next one, the way out of the demo
 * into a real project.
 */

import { useContext, useEffect, useState, type ReactNode } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ArrowRight, Eye, EyeOff, Plus, RotateCcw, X } from 'lucide-react'
import { ActiveProjectContext } from '@/components/active-project-context'
import { Chip } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { Button } from '@/components/ui/button'
import { useCanManageProject, useCanWriteProject } from '@/lib/permissions'
import { useCoachPresence, useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import {
  CHAPTER_TITLES,
  SCENARIO_HINT_COPY,
  scenarioStepIndex,
  type ChapterId,
  type ChapterListEntry,
  type ScenarioHint,
  type ScenarioStep,
} from './scenarioModel'
import { useWelcomeDismissed } from './welcomeDismissal'
import { currentOrgSlug, projectPath, workspacePath } from '@/lib/navigation'

const REGION_LABEL = 'Demo scenario'

/**
 * How long the user must sit on the step's route with no coach mark mounted
 * before the strip says so. Route transitions unmount one surface's mark before
 * the next surface mounts its own, so an instant message would flicker.
 */
const MISSING_TARGET_DELAY_MS = 1000

const MISSING_TARGET_COPY =
  "The highlighted control isn't visible — it may be filtered out, below the fold, or already handled."

/** Only for whoever can reset the demo: offering it to anyone else was a dead end. */
const RESET_RESTORES_COPY = 'Resetting the demo project restores every guided example.'

/**
 * A viewer on a step's surface has no coach mark because the control is not
 * rendered for their role (#251): "isn't visible … reset" read as a bug
 * and pointed at a Reset they cannot use either.
 */
const NEEDS_EDITOR_COPY =
  'This step needs edit access — ask an owner for it, or keep exploring the rest of the demo.'

/** True only after `value` has held true for `delayMs` without interruption. */
function useDeferredFlag(value: boolean, delayMs: number): boolean {
  const [deferred, setDeferred] = useState(false)
  useEffect(() => {
    // A zero-delay timer (rather than a sync set) also handles the reset, so
    // the effect never calls setState synchronously.
    const timer = window.setTimeout(() => setDeferred(value), value ? delayMs : 0)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return value && deferred
}

/**
 * The strip is a segment of the demo banner's row, not a card of its own:
 * the two stacked pushed the page's title far down every screen. On
 * one line from `lg` up — the long text shrinks and truncates instead of
 * wrapping — and a full-width block in the phone panel, which wraps.
 * `data-demo-scenario` is how the banner knows the slot is filled, to give up
 * its own labels only then.
 */
function StripShell({ children }: { children: ReactNode }) {
  return (
    <section
      aria-label={REGION_LABEL}
      data-demo-scenario=""
      className="flex min-w-0 grow basis-full flex-wrap items-center gap-x-2 gap-y-1.5 border-t pt-1.5 lg:basis-0 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-3 border-warning"
    >
      {children}
    </section>
  )
}

/** Visible from `2xl`, or in the phone panel; an icon alone in between. */
const STRIP_LABEL = 'lg:sr-only 2xl:not-sr-only'

function ChapterProgress({ index, total }: { index: number; total: number }) {
  return (
    <div className="flex items-center gap-1 lg:hidden xl:flex" aria-hidden="true">
      {Array.from({ length: total }, (_, position) => (
        <span
          key={position}
          className="h-1 w-5 rounded-full motion-safe:transition-colors"
          style={{ background: position <= index ? 'var(--accent)' : 'var(--border-subtle)' }}
        />
      ))}
    </div>
  )
}

interface ActiveStripProps {
  chapter: ChapterId
  step: ScenarioStep
  index: number
  total: number
  hint?: ScenarioHint
  isWatching: boolean
  /** The user is on the step's surface but no coach mark is mounted there. */
  targetMissing: boolean
  /**
   * The user is already on the page the step's link opens (#251): the
   * "Open Scans" button there was a no-op, and its room goes to the
   * instruction instead.
   */
  onStepPage: boolean
  /** What to say when the mark is missing — the reason differs by role. */
  missingCopy: string
  /** On-surface callouts are silenced — offer the way back. */
  hintsMuted: boolean
  /** The step has an on-surface mark to silence. */
  hasMark: boolean
  onShowHints: () => void
  onHideHints: () => void
  onDismiss: () => void
}

function ActiveStrip({
  chapter,
  step,
  index,
  total,
  hint,
  isWatching,
  targetMissing,
  onStepPage,
  missingCopy,
  hintsMuted,
  hasMark,
  onShowHints,
  onHideHints,
  onDismiss,
}: ActiveStripProps) {
  return (
    <StripShell>
      <Chip tone="accent" size="xs" className="shrink-0">
        {CHAPTER_TITLES[chapter]}
      </Chip>
      <ChapterProgress index={index} total={total} />

      {/* Grows from a zero basis, so on the one-line row it takes what is
          left rather than pushing the controls onto a second line. */}
      <div
        aria-live="polite"
        className="flex min-w-0 grow basis-full flex-wrap items-center gap-x-2 gap-y-1 lg:basis-0 lg:flex-nowrap"
      >
        <span className="flex shrink-0 items-center gap-1.5 text-body-sm font-medium whitespace-nowrap">
          <Dot tone="accent" pulse={isWatching} />
          {step.title}
        </span>
        <Chip tone="neutral" size="xs" className="shrink-0">
          Step {index + 1} of {total}
        </Chip>
        {/* Cut to the row's width on a desktop, whole in the DOM (and so to a
            screen reader), and whole on hover. */}
        <span
          className="min-w-0 text-caption leading-[1.45] lg:truncate text-fg-secondary"
          title={step.instruction}
        >
          {step.instruction}
        </span>
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        {!onStepPage && (
          <Button asChild size="xs">
            <Link to={step.to}>
              {step.ctaLabel}
              <ArrowRight className="h-3 w-3" />
            </Link>
          </Button>
        )}
        {/* "Hide hints" is the coach card's only control and it used to be a
            one-way door: nothing turned the marks back on for the rest of the
            chapter. */}
        {hintsMuted && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={onShowHints}
            style={{ color: 'var(--fg-subtle)' }}
          >
            <Eye className="h-3 w-3" aria-hidden="true" />
            Show hints
          </Button>
        )}
        {/* The same toggle the coach card offers, here in the normal tab
            order: the card is portalled to the end of <body>, so a keyboard
            user had to Tab through the whole page to reach it. */}
        {!hintsMuted && hasMark && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={onHideHints}
            style={{ color: 'var(--fg-subtle)' }}
            // An aria-label rather than a hidden span: a name is built from
            // each element's trimmed text, so " on the page" in a span came
            // out as "Hide hintson the page".
            aria-label="Hide hints on the page"
            title="Hide hints on the page"
          >
            <EyeOff className="h-3 w-3" aria-hidden="true" />
            <span className={STRIP_LABEL}>Hide hints</span>
          </Button>
        )}
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={onDismiss}
          style={{ color: 'var(--fg-subtle)' }}
          title="Dismiss"
        >
          <X className="h-3 w-3" aria-hidden="true" />
          <span className={STRIP_LABEL}>Dismiss</span>
        </Button>
      </div>

      {/* Announced on its own: a regression is news, and the step text it sits
          under may not have changed. A line of its own under the row: the
          exception may cost height, the normal state does not. */}
      {hint && (
        <p role="status" className="basis-full text-caption text-warning">
          {SCENARIO_HINT_COPY[hint]}
        </p>
      )}

      {targetMissing && (
        <p className="basis-full text-caption text-fg-secondary">
          {missingCopy}
        </p>
      )}
    </StripShell>
  )
}

interface CompletedStripProps {
  chapter: ChapterId
  nextChapter: ChapterListEntry | null
  onStartNext: (chapter: ChapterId) => void
  onRestart: () => void
  onDismiss: () => void
}

function CompletedStrip({
  chapter,
  nextChapter,
  onStartNext,
  onRestart,
  onDismiss,
}: CompletedStripProps) {
  return (
    <StripShell>
      <Dot tone="success" />
      <p
        aria-live="polite"
        className="min-w-0 grow basis-full text-body-sm font-medium lg:basis-0 lg:truncate"
      >
        Chapter complete: {CHAPTER_TITLES[chapter]}.{' '}
        <span className="font-normal text-fg-secondary">
          {nextChapter
            ? 'Keep going — the next chapter picks up from here.'
            : 'That was the last one — you have walked the whole product. Point it at your own warehouse next.'}
        </span>
      </p>
      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        {nextChapter ? (
          <Button asChild size="xs">
            {/* Starting on click, before the Link navigates, so the user lands
                on the new chapter's surface with its first step already live. */}
            <Link to={nextChapter.to} onClick={() => onStartNext(nextChapter.id)}>
              Next: {nextChapter.title}
              <ArrowRight className="h-3 w-3" />
            </Link>
          </Button>
        ) : (
          /* The moment of highest intent used to end in Restart + Dismiss, with
             nothing in the whole demo pointing at the real product.
             The dashboard, not Data sources: creating the project comes first,
             and a demo-scoped link straight to the global connection page was
             deliberately removed. */
          <Button asChild size="xs">
            <Link to={workspacePath()}>
              <Plus className="h-3 w-3" />
              Create a real project
            </Link>
          </Button>
        )}
        <Button type="button" variant="outline" size="xs" onClick={onRestart} title="Restart chapter">
          <RotateCcw className="h-3 w-3" aria-hidden="true" />
          <span className={STRIP_LABEL}>Restart chapter</span>
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={onDismiss}
          style={{ color: 'var(--fg-subtle)' }}
          title="Dismiss"
        >
          <X className="h-3 w-3" aria-hidden="true" />
          <span className={STRIP_LABEL}>Dismiss</span>
        </Button>
      </div>
    </StripShell>
  )
}

/**
 * Renders on exactly two conditions: a chapter is active, or the chapter the
 * user was in completed and has not been dismissed. Everything else —
 * dismissed, still seeding, not a demo, no provider at all — is nothing.
 *
 * The two branches cannot leak into a non-demo project: outside a ready demo
 * the context is inert, which means `active` is false and `activeChapter` is
 * null. Dismissing the completed strip keeps the chapter's status 'completed'
 * and only clears the active pointer, so the branch below simply stops
 * rendering without demoting a finished chapter.
 */
/** Every query param the step's link names is present in `search` with the same value. */
function searchMatches(search: string, to: string): boolean {
  const query = to.split('?')[1]
  if (!query) return true
  const current = new URLSearchParams(search)
  return [...new URLSearchParams(query)].every(([key, value]) => current.get(key) === value)
}

export function DemoScenarioStrip() {
  const { active, state, activeChapter, step, steps, nextChapter, isWatching, hintsMuted } =
    useDemoScenario()
  const { startChapter, restartChapter, dismissChapter, muteHints, unmuteHints } =
    useDemoScenarioActions()
  const { present } = useCoachPresence()
  const location = useLocation()
  const { slug } = useParams()
  const welcomeDismissed = useWelcomeDismissed(slug ?? '')
  const canEdit = useCanWriteProject()
  const canManage = useCanManageProject(useContext(ActiveProjectContext))

  // The user is standing on the step's own surface, yet no coach mark for the
  // step is mounted — the control is filtered out or not rendered at all. A
  // step that names a tab is on its surface only on that tab: from another one
  // the link below takes the user there, and the warning would be wrong.
  // Muting hints silences this too: it keys off the same visibility the marks
  // themselves report. Steps without an on-surface anchor (deep-link and
  // explore steps) expect no mark, so they stay quiet.
  const stepPath = step.to.split('?')[0] ?? step.to
  const targetMissing =
    active &&
    !hintsMuted &&
    step.coach !== undefined &&
    location.pathname.startsWith(stepPath) &&
    searchMatches(location.search, step.to) &&
    !present.has(step.id)
  const showTargetMissing = useDeferredFlag(targetMissing, MISSING_TARGET_DELAY_MS)
  // The page itself, not a page under it: from a scan's detail the link back
  // to the Scans list still goes somewhere. A step that names a tab
  // (`?section=monitors`) is on its page only on that tab, so from another tab
  // the link stays and takes the user to the control.
  const onStepPage =
    location.pathname.replace(/\/$/, '') === stepPath.replace(/\/$/, '') &&
    searchMatches(location.search, step.to)
  const missingCopy = !canEdit
    ? NEEDS_EDITOR_COPY
    : canManage
      ? `${MISSING_TARGET_COPY} ${RESET_RESTORES_COPY}`
      : MISSING_TARGET_COPY

  // First visit: the Overview's welcome panel already offers every
  // chapter, and banner + strip + panel stacked three demo blocks above the
  // page title. Until the user engages, the panel stands in for the strip
  // there instead of beside it. Engaging is more than leaving the first step:
  // a user who picked "Run the live loop" or pressed Restart is on that very
  // step too, and must keep the strip that coaches it.
  const welcomeShowing =
    slug !== undefined && !welcomeDismissed && location.pathname === projectPath(currentOrgSlug(), slug, '/overview')
  const untouched =
    !state.engaged && activeChapter === 'live-loop' && step.id === 'live-loop/run-scan'
  if (welcomeShowing && untouched) return null

  if (active && activeChapter) {
    return (
      <ActiveStrip
        chapter={activeChapter}
        step={step}
        index={scenarioStepIndex(state)}
        total={steps.length}
        hint={state.chapters[activeChapter]?.hint}
        isWatching={isWatching}
        targetMissing={showTargetMissing}
        onStepPage={onStepPage}
        missingCopy={missingCopy}
        hintsMuted={hintsMuted}
        hasMark={step.coach !== undefined}
        onShowHints={unmuteHints}
        onHideHints={muteHints}
        onDismiss={() => dismissChapter(activeChapter)}
      />
    )
  }

  if (activeChapter && state.chapters[activeChapter]?.status === 'completed') {
    return (
      <CompletedStrip
        chapter={activeChapter}
        nextChapter={nextChapter}
        onStartNext={startChapter}
        onRestart={() => restartChapter(activeChapter)}
        onDismiss={() => dismissChapter(activeChapter)}
      />
    )
  }

  return null
}
