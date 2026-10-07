/**
 * Demo welcome surface on the Overview.
 *
 * A freshly-created demo lands here (not Events). The panel orients the user
 * and hands them to the one guided path: the chapters. It is dismissible and
 * remembered per project so it doesn't become permanent chrome.
 *
 * One row, never a card that expands (#251): it used to open into
 * ~500px of tour button, links, the seven chapters and the metric building
 * blocks — the same chapter list the "Tour & chapters" dialog shows, so a new
 * user met two lists and three step counters before the product. Now it is a
 * sentence and two ways in: start the next chapter where you are, or browse
 * them all in that dialog, which also holds the quick overview and the index
 * of every surface and building block.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowRight, Compass, Sparkles, X } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import type { Project } from '@/types'
import { useDemoScenario, useDemoScenarioActions } from './demoScenarioContext'
import { ProductTour } from './ProductTour'
import type { ChapterListEntry } from './scenarioModel'
import { setWelcomeDismissed, useWelcomeDismissed } from './welcomeDismissal'
import { usePublicDemo } from '@/lib/deploymentMode'
import { workspacePath } from '@/lib/navigation'

export function DemoWelcomePanel({ project }: { project: Project }) {
  const [tourOpen, setTourOpen] = useState(false)
  const navigate = useNavigate()
  const { available, chapters, state } = useDemoScenario()
  const { startChapter } = useDemoScenarioActions()
  const dismissed = useWelcomeDismissed(project.slug)
  const publicDemo = usePublicDemo()

  if (dismissed) return null

  // The chapter the user is in, else the first one not finished yet. A fresh
  // demo has "Run the live loop" active before anyone touched it, so it is
  // "Start" until the user engages, and "Continue" only after.
  const next: ChapterListEntry | undefined = available
    ? (chapters.find((chapter) => chapter.status === 'active') ??
      chapters.find((chapter) => chapter.status !== 'completed'))
    : undefined
  const started = next !== undefined && next.status !== 'not_started' && state.engaged === true

  /**
   * One unconfirmed click puts the panel away, and it sits right beside the
   * row's buttons — easy to hit by accident on a touch screen. So
   * the dismissal offers Undo and names the way back for later.
   */
  function dismiss(): void {
    setWelcomeDismissed(project.slug, true)
    toast('Demo welcome hidden', {
      id: `demo-welcome-dismissed:${project.slug}`,
      description: 'Bring it back any time from "Tour & chapters" in the demo banner.',
      action: { label: 'Undo', onClick: () => setWelcomeDismissed(project.slug, false) },
    })
  }

  /** Start (or resume) a chapter and drop the user on its first surface. */
  function openChapter(chapter: ChapterListEntry): void {
    startChapter(chapter.id)
    void navigate(chapter.to)
  }

  return (
    // --fg-subtle, never --fg-faint, for text on this --accent-soft fill: faint
    // measures about 4.05:1 on it, below AA.
    <section
      aria-labelledby="demo-welcome-heading"
      className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border px-3 py-1.5 bg-accent-soft border-accent"
    >
      <div className="flex min-w-0 items-center gap-2">
        <Sparkles className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <h2 id="demo-welcome-heading" className="text-body-sm font-semibold">
          Welcome to your demo workspace
        </h2>
      </div>
      {/* No "Local synthetic data" badge here: the demo banner right above
          already carries it on every surface. */}
      <p className="hidden min-w-0 text-caption md:block text-fg-secondary">
        Everything runs on a local, synthetic warehouse — nothing outside is touched.
      </p>

      <div className="ml-auto flex flex-wrap items-center gap-1.5">
        {next && (
          <Button type="button" size="xs" onClick={() => openChapter(next)}>
            {started ? 'Continue' : 'Start'}: {next.title}
            <ArrowRight className="h-3 w-3" aria-hidden="true" />
          </Button>
        )}
        <Button type="button" size="xs" variant="outline" onClick={() => setTourOpen(true)}>
          <Compass className="h-3 w-3" aria-hidden="true" />
          {available ? 'Browse chapters' : 'Take the tour'}
        </Button>
        {/* The demo never pointed at the real product, so the funnel it is
            the front of ended in a dead stop. The dashboard is
            where "New project — start empty and connect your own warehouse"
            lives. Not on a public demo: the server refuses a blank project
            there, and the link led nowhere. */}
        {!publicDemo && (
          <Button asChild size="xs" variant="ghost">
            <Link to={workspacePath()}>Create a real project</Link>
          </Button>
        )}
        {/* A 36px target, pulled into the row's padding so the row
            stays one line high. */}
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss demo welcome"
          className="-my-1 -mr-2 flex h-9 w-9 items-center justify-center rounded-md transition-colors hover:bg-[var(--surface-hover)] text-fg-tertiary"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      {/* Mounted only while open, for the same reason DemoBanner's copy is: the
          tour reads its persisted step once, in a useState initializer, and this
          panel and the banner are both on the Overview. Held mounted, this
          instance kept the index it captured at first render, so opening it
          after stepping the banner's copy forward reopened at step 1 and its
          first Next wrote that back over the saved position. */}
      {tourOpen && <ProductTour slug={project.slug} open onOpenChange={setTourOpen} />}
    </section>
  )
}
