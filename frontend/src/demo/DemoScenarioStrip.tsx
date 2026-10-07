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
 * (EndOfDemoLink): a real project, or on a public demo the quick start.
 *
 * What to do on the page is the demo guide's to say — beside the coach mark
 * on the step's control (ScenarioCoachMark), or, with no mark on screen, from
 * the guide host (DemoGuideHost), which also explains a control that is not
 * there.
 */

import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ArrowRight, Eye, EyeOff, RotateCcw, X } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { Button } from '@/components/ui/button'
import {
  entryPresenceKey,
  followUpPresenceKey,
  useCoachPresence,
  useDemoScenario,
  useDemoScenarioActions,
} from './demoScenarioContext'
import {
  CHAPTER_TITLES,
  SCENARIO_HINT_COPY,
  scenarioStepIndex,
  type ChapterId,
  type ChapterListEntry,
  type ScenarioHint,
  type ScenarioStep,
} from './scenarioModel'
import { EndOfDemoLink } from './EndOfDemoLink'
import { isOnStepPage, useWelcomeStandsIn } from './stepLocation'

const REGION_LABEL = 'Demo scenario'

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
      className="@container/strip flex min-w-0 grow basis-full flex-wrap items-center gap-x-2 gap-y-1.5 border-t pt-1.5 lg:basis-0 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-3 border-warning"
    >
      {children}
    </section>
  )
}

/*
 * What the one-line strip keeps as it narrows — by its own width, not the
 * screen's: the activity rail open beside the page took 304px from the row
 * on screens wide enough to have brought every label back, and the step
 * title ran into the controls. Below `lg` (the phone panel) it wraps, and all
 * of it shows.
 */
/** Button labels where the strip has room for them; an icon alone otherwise. */
const STRIP_LABEL = 'lg:@max-[900px]/strip:sr-only'

function ChapterProgress({ index, total }: { index: number; total: number }) {
  return (
    <div className="flex items-center gap-1 lg:@max-[720px]/strip:hidden" aria-hidden="true">
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
  /**
   * Offer the step's link. Not on the page it opens (#251): the "Open Scans"
   * button there was a no-op, and its room goes to the instruction instead.
   * Nor while a coach mark is on the step's control or the way back to it:
   * the user is where the step happens, whatever the address says.
   */
  showLink: boolean
  /** On-surface callouts are silenced — offer the way back. */
  hintsMuted: boolean
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
  showLink,
  hintsMuted,
  onShowHints,
  onHideHints,
  onDismiss,
}: ActiveStripProps) {
  return (
    <StripShell>
      <Chip tone="accent" size="xs" className="shrink-0 lg:@max-[500px]/strip:hidden">
        {CHAPTER_TITLES[chapter]}
      </Chip>
      <ChapterProgress index={index} total={total} />

      {/* Grows from a zero basis, so on the one-line row it takes what is
          left rather than pushing the controls onto a second line. */}
      <div
        aria-live="polite"
        className="flex min-w-0 grow basis-full flex-wrap items-center gap-x-2 gap-y-1 lg:basis-0 lg:flex-nowrap"
      >
        {/* Cut short, never pushed over its neighbours, when the row is tight. */}
        <span className="flex min-w-0 items-center gap-1.5 text-body-sm font-medium whitespace-nowrap">
          <Dot tone="accent" pulse={isWatching} />
          <span className="truncate">{step.title}</span>
        </span>
        <Chip tone="neutral" size="xs" className="shrink-0">
          Step {index + 1} of {total}
        </Chip>
        {/* Cut to the row's width on a desktop, whole in the DOM (and so to a
            screen reader), and whole on hover. A strip too narrow for a
            useful fragment of it leaves it to the demo guide. */}
        <span
          // Only what the title and the step count leave: the title goes last.
          className="min-w-0 text-caption leading-[1.45] lg:flex-1 lg:basis-0 lg:truncate lg:@max-[620px]/strip:sr-only text-fg-secondary"
          title={step.instruction}
        >
          {step.instruction}
        </span>
      </div>

      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        {showLink && (
          <Button asChild size="xs">
            <Link to={step.to}>
              {step.ctaLabel}
              <ArrowRight className="h-3 w-3" />
            </Link>
          </Button>
        )}
        {/* "Hide hints" on the demo guide used to be a one-way door: nothing
            turned the marks back on for the rest of the chapter. */}
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
        {/* The same toggle the demo guide offers, here in the normal tab
            order: the guide is portalled to the end of <body>, so a keyboard
            user had to Tab through the whole page to reach it. Offered on
            every step: the guide speaks on every one. */}
        {!hintsMuted && (
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
             nothing in the whole demo pointing at the real product. */
          <EndOfDemoLink />
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
export function DemoScenarioStrip() {
  const { active, state, activeChapter, step, steps, nextChapter, isWatching, hintsMuted } =
    useDemoScenario()
  const { startChapter, restartChapter, dismissChapter, muteHints, unmuteHints } =
    useDemoScenarioActions()
  const { present } = useCoachPresence()
  const location = useLocation()
  const welcomeStandsIn = useWelcomeStandsIn()

  // A mark on the step's control — on the way back to it, or on its second
  // gesture — means the user is where the step happens: the branch detail
  // opens from the list on the same page, at an address the link does not name.
  const markPresent =
    present.has(step.id) ||
    present.has(entryPresenceKey(step.id)) ||
    present.has(followUpPresenceKey(step.id))
  const showLink = !isOnStepPage(location, step.to) && !markPresent

  if (welcomeStandsIn) return null

  if (active && activeChapter) {
    return (
      <ActiveStrip
        chapter={activeChapter}
        step={step}
        index={scenarioStepIndex(state)}
        total={steps.length}
        hint={state.chapters[activeChapter]?.hint}
        isWatching={isWatching}
        showLink={showLink}
        hintsMuted={hintsMuted}
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
