/**
 * Capability-aware product tour (tripl-2su6.9).
 *
 * A stepper through the core surfaces. Each step deep-links to the REAL surface,
 * so following the tour closes the dialog — which means progress has to survive
 * that, or the tour is unfollowable. It used to reset to step one on
 * every close, so opening a step's surface and coming back put you straight back
 * at the beginning: a link list wearing a stepper's clothes (tripl-2su6.18).
 *
 * Now: opening a step's surface ADVANCES the tour (visiting is progress, not
 * abandonment), the position is persisted per project, and a plain dismissal
 * keeps your place. Finishing — or opening the last step's surface — resets it,
 * so a completed tour starts fresh next time.
 *
 * And the tour does not vanish when it steps aside (#251 JR-22): "Open X" docks
 * it as a small card on X's page — "Step 4 of 11", X's name, Next — so the
 * reader walks the surfaces one Next at a time instead of reopening the dialog
 * from the banner after each one. The dock (`TourDock`, mounted by the banner on
 * every demo surface) can reopen the dialog or be closed; closing it keeps the
 * place like any other dismissal. It stands aside while a chapter is running,
 * so only one thing coaches at a time.
 *
 * A footer index still lists every surface plus the metric building blocks so
 * they stay directly reachable without paging; using it leaves the stepper's
 * position alone. It sits behind a disclosure (DEMO-20): shown on every step it
 * added sixteen links to every keyboard pass through the dialog.
 *
 * Paging is announced (DEMO-19): Next and Back swap the step in place while
 * focus stays on the button, so a polite live region says where the reader
 * landed. The primary button is one element whose label changes (Next →
 * Finish), and Back is aria-disabled rather than disabled on the first step,
 * so the focused control never vanishes from under the keyboard.
 */

import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, ArrowRight, ChevronDown, Compass, Search, X } from 'lucide-react'
import { useCommandPalette } from '@/components/command-palette-context'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { ChapterPicker } from './ChapterPicker'
import { TOUR_STORAGE_PREFIX } from './demoLocalState'
import { useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import type { ChapterListEntry } from './scenarioModel'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { setTourDock, useTourDock } from './tourDock'
import { buildMetricBuildingBlocks, buildTourSteps, type TourStep } from './tourSteps'
import { setWelcomeDismissed, useWelcomeDismissed } from './welcomeDismissal'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { orgStorageKey } from '@/lib/activeOrg'

const STORAGE_PREFIX = TOUR_STORAGE_PREFIX

/** Persisted step, clamped to the current tour's length; 0 on anything unusable. */
function readStoredStep(slug: string, stepCount: number): number {
  if (typeof window === 'undefined') return 0
  try {
    const raw = window.localStorage.getItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`))
    if (raw === null) return 0
    const parsed = Number.parseInt(raw, 10)
    if (!Number.isInteger(parsed) || parsed < 0 || parsed >= stepCount) return 0
    return parsed
  } catch {
    return 0
  }
}

function writeStoredStep(slug: string, step: number): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(orgStorageKey(`${STORAGE_PREFIX}${slug}`), String(step))
  } catch {
    /* ignore — a tour that cannot remember its place still works */
  }
}

interface ProductTourProps {
  slug: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** The index lists each surface once; action steps have no surface of their own. */
function indexEntries(steps: readonly TourStep[]): TourStep[] {
  const seen = new Set<string>()
  return steps.filter((step) => {
    if (step.action || seen.has(step.to)) return false
    seen.add(step.to)
    return true
  })
}

const INDEX_LINK_CLASS =
  'rounded-full px-2.5 py-1 text-caption font-medium no-underline transition-colors hover:bg-[var(--surface-hover)]'

export function ProductTour({ slug, open, onOpenChange }: ProductTourProps) {
  const steps = buildTourSteps(slug)
  const blocks = buildMetricBuildingBlocks(slug)
  const navigate = useNavigate()
  const palette = useCommandPalette()
  const { available: scenarioAvailable, chapters } = useDemoScenario()
  const { startChapter } = useDemoScenarioActions()
  const welcomeDismissed = useWelcomeDismissed(slug)
  const [index, setIndexState] = useState(() => readStoredStep(slug, steps.length))
  const [announcement, setAnnouncement] = useState('')
  const [indexOpen, setIndexOpen] = useState(false)
  const indexId = useId()
  // Set when the tour hands focus to the command palette, so closing the
  // dialog does not pull it back to the tour's trigger.
  const handingOffRef = useRef(false)
  const step = steps[Math.min(index, steps.length - 1)] ?? steps[0]
  const isFirst = index === 0
  const isLast = index === steps.length - 1

  // Another tab (or the other copy of the tour on the Overview) moving the
  // stored step: follow it rather than writing a stale position back (DEMO-16).
  useEffect(() => {
    const key = orgStorageKey(`${STORAGE_PREFIX}${slug}`)
    const onStorage = (event: StorageEvent) => {
      if (event.key === key || event.key === null) setIndexState(readStoredStep(slug, steps.length))
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [slug, steps.length])

  const goTo = (next: number) => {
    const clamped = Math.min(Math.max(0, next), steps.length - 1)
    setIndexState(clamped)
    writeStoredStep(slug, clamped)
    const target = steps[clamped]
    if (target) setAnnouncement(`Step ${clamped + 1} of ${steps.length}: ${target.title}`)
  }

  /** Dismiss without losing your place (Escape, the X, the footer index). */
  const dismiss = () => onOpenChange(false)

  /** The tour is done: close, and start from the top next time. */
  const finish = () => {
    goTo(0)
    setTourDock(slug, null)
    onOpenChange(false)
  }

  /**
   * Opening the step's surface IS the step being done, so the tour advances and
   * the dialog steps aside — into the dock on that surface (#251 JR-22), whose
   * Next carries on from there. Reopened, the dialog waits on the next one.
   */
  const openStepSurface = () => {
    if (isLast) finish()
    else {
      setTourDock(slug, index)
      goTo(index + 1)
      onOpenChange(false)
    }
  }

  /** A step whose surface is the command palette opens it rather than a page (DEMO-18). */
  const runStepAction = () => {
    handingOffRef.current = true
    openStepSurface()
    palette.setOpen(true)
  }

  /**
   * The tour shows the surfaces; a scenario chapter makes one thing happen on
   * them. Picking one hands the user over to the strip, which coaches from
   * here on, and drops them on the chapter's first surface.
   */
  const openChapter = (chapterEntry: ChapterListEntry) => {
    setTourDock(slug, null)
    startChapter(chapterEntry.id)
    onOpenChange(false)
    navigate(chapterEntry.to)
  }

  /** Bring the welcome panel back — its own intent, no longer bundled with opening the tour (DEMO-26). */
  const showWelcome = () => {
    setWelcomeDismissed(slug, false)
    onOpenChange(false)
    navigate(projectPath(currentOrgSlug(), slug, '/overview'))
  }

  const surfaces = indexEntries(steps)

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : dismiss())}>
      <DialogContent
        className="min-w-0 max-w-lg overflow-x-hidden"
        onCloseAutoFocus={(event) => {
          if (handingOffRef.current) event.preventDefault()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Compass className="h-4 w-4 text-accent" />
            Product tour
          </DialogTitle>
          <DialogDescription>
            {`Step ${index + 1} of ${steps.length} · a quick guided path through tripl.`}
          </DialogDescription>
        </DialogHeader>

        {/* Says where Next / Back landed; empty until the user pages, so
            opening the dialog is announced once, by its title and description. */}
        <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </p>

        {/* Chapters first (#251 SH-3 / JR-23): they are the guided path, and
            the slideshow below only names the surfaces. Reading about a surface
            is not the same as making it do something. Each chapter hands off to
            the coached scenario on one product area; click = start (or resume)
            it and land on its first surface. */}
        {scenarioAvailable && (
          <div
            className="min-w-0 rounded-lg border p-3 bg-accent-soft border-border-subtle"
          >
            <p className="mb-2 text-body-sm font-medium">
              Hands-on chapters{' '}
              <span className="font-normal text-fg-secondary">
                — pick one, and the demo bar coaches you through it.
              </span>
            </p>
            <ChapterPicker chapters={chapters} onPick={openChapter} />
          </div>
        )}

        <div
          className="rounded-lg border p-4 bg-bg-sunken border-border-subtle"
        >
          {/* Named apart from the chapters above, so "tour" and "chapter" are
              not two words for one thing (#251 JR-23). */}
          <p className="mb-2 micro-label text-fg-tertiary">
            Quick overview
          </p>
          <div className="flex items-center gap-2">
            {/* The sidebar group the page is in; a step with no sidebar item
                (search) has none, and prints none. */}
            {step.area && (
              <Chip tone="accent" size="xs">
                {step.area}
              </Chip>
            )}
            <span className="text-body font-semibold">{step.title}</span>
          </div>
          <p className="mt-2 text-body-sm leading-[1.5] text-fg-tertiary">
            {step.blurb}
          </p>
          {step.action === 'open-command-palette' ? (
            <Button type="button" size="sm" className="mt-3" onClick={runStepAction}>
              <Search className="h-3.5 w-3.5" />
              Open {step.title}
            </Button>
          ) : (
            <Button asChild size="sm" className="mt-3" onClick={openStepSurface}>
              <Link to={step.to}>
                Open {step.title}
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          )}
        </div>

        <div className="flex items-center justify-between gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              if (!isFirst) goTo(index - 1)
            }}
            aria-disabled={isFirst || undefined}
            className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Back
          </Button>
          <Button type="button" size="sm" onClick={isLast ? finish : () => goTo(index + 1)}>
            {isLast ? 'Finish' : 'Next'}
            {!isLast && <ArrowRight className="h-3.5 w-3.5" />}
          </Button>
        </div>

        {welcomeDismissed && (
          <button
            type="button"
            onClick={showWelcome}
            className="self-start rounded-sm px-1 text-body-sm font-medium underline-offset-2 hover:underline text-accent"
          >
            Show the welcome panel on Overview
          </button>
        )}

        {/* Direct index — every surface + the metric building blocks are one
            click away, regardless of the stepper position. Behind a
            disclosure, so it costs one Tab stop until it is wanted. */}
        <div className="min-w-0 border-t pt-3 border-border-subtle">
          <button
            type="button"
            onClick={() => setIndexOpen((value) => !value)}
            aria-expanded={indexOpen}
            aria-controls={indexId}
            className="flex items-center gap-1 rounded-sm px-1 py-0.5 text-caption font-medium transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
          >
            All surfaces
            <ChevronDown
              className="h-3 w-3 transition-transform"
              style={{ transform: indexOpen ? 'rotate(180deg)' : 'none' }}
            />
          </button>
          {indexOpen && (
            <div id={indexId} className="mt-2">
              <p className="mb-2 micro-label text-fg-tertiary">
                Jump to any surface
              </p>
              <ul className="flex flex-wrap gap-1.5">
                {surfaces.map((s) => (
                  <li key={s.id}>
                    <Link
                      to={s.to}
                      onClick={dismiss}
                      aria-describedby={`${indexId}-${s.id}`}
                      className={INDEX_LINK_CLASS}
                      style={{ background: 'var(--surface)', color: 'var(--fg-muted)', border: '1px solid var(--border-subtle)' }}
                    >
                      {s.title}
                    </Link>
                    {/* The blurb used to live in `title`, which touch and
                        screen-reader users never get. */}
                    <span id={`${indexId}-${s.id}`} hidden>
                      {s.blurb}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="mb-2 mt-3 micro-label text-fg-tertiary">
                Metric building blocks
              </p>
              <ul className="flex flex-wrap gap-1.5">
                {blocks.map((b) => (
                  <li key={b.id}>
                    <Link
                      to={b.to}
                      onClick={dismiss}
                      title={b.blurb}
                      aria-describedby={`${indexId}-block-${b.id}`}
                      className={INDEX_LINK_CLASS}
                      style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}
                    >
                      {b.label}
                    </Link>
                    <span id={`${indexId}-block-${b.id}`} hidden>
                      {b.blurb}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The tour, docked on the surface its "Open X" led to (#251 JR-22).
 *
 * A hint, not a dialog, like the scenario's docked coach card: it takes no
 * focus and traps none. Bottom right, clear of the tweaks FAB (`bottom-[68px]`,
 * as the coach card), under dialogs (`z-40`); a full-width strip on a phone.
 * Next walks on to the following surface — or opens search, for the step that
 * lives in the command palette — and keeps the dialog's stored place in step
 * with it: the step after the one on screen, or the top once the last is
 * reached. Nothing renders while no step is docked, or while a chapter runs.
 */
export function TourDock({ slug, onOpenTour }: { slug: string; onOpenTour: () => void }) {
  const docked = useTourDock(slug)
  const navigate = useNavigate()
  const palette = useCommandPalette()
  const { active: chapterRunning } = useDemoScenario()
  const steps = buildTourSteps(slug)
  const step = docked === null ? undefined : steps[docked]
  if (docked === null || !step || chapterRunning) return null
  const next = steps[docked + 1]

  const goNext = () => {
    if (!next) {
      setTourDock(slug, null)
      return
    }
    const target = docked + 1
    setTourDock(slug, target)
    writeStoredStep(slug, target + 1 < steps.length ? target + 1 : 0)
    if (next.action === 'open-command-palette') palette.setOpen(true)
    else navigate(next.to)
  }

  return createPortal(
    <section
      aria-label="Product tour"
      data-tour-dock=""
      className="fixed bottom-[68px] left-3 right-3 z-40 rounded-lg border p-3 shadow-lg sm:left-auto sm:right-4 sm:w-72 bg-bg-elevated border-accent"
    >
      <div className="flex items-start gap-2">
        <Compass
          className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <p className="micro-label text-fg-tertiary">
            Tour · step {docked + 1} of {steps.length}
          </p>
          <p className="mt-0.5 truncate text-body-sm font-medium">{step.title}</p>
        </div>
        <button
          type="button"
          onClick={() => setTourDock(slug, null)}
          aria-label="Close the tour"
          title="Close the tour"
          className="-mr-1 -mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-end gap-1.5">
        <Button type="button" variant="ghost" size="sm" onClick={onOpenTour}>
          All steps
        </Button>
        <Button type="button" size="sm" onClick={goNext}>
          {next ? `Next: ${next.title}` : 'Finish'}
          {next && <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />}
        </Button>
      </div>
    </section>,
    document.body,
  )
}
