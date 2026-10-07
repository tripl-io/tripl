/**
 * The demo guide whenever no coach mark is speaking.
 *
 * A mark on the step's control shows the guide itself, beside its ring. The
 * rest of a running chapter used to have nothing on the page: a step whose
 * control is on another page, a step that is only a visit ("Click Coverage in
 * the sidebar"), a control filtered out of view. Visitors stood there not
 * knowing where to click. The host fills those moments with the same guide —
 * what the step is, the gesture, the way there (a link to the step's page and
 * a ring on its item in the sidebar, or on the page's own tab when the step
 * lives in another section of this page) — and, on the page with the control
 * nowhere to be seen, why.
 *
 * It also keeps the guide on the page where testers lost it: with hints
 * hidden, the guide's face stays in its corner and brings them back; once a
 * chapter lands, the guide says so and offers the next one, where the strip
 * alone used to — scrolled away above the page.
 *
 * Mounted by the shell beside the demo banner, for demo projects. It renders
 * nothing while a mark for the step is on screen (its own, or the way back to
 * it), while no chapter runs, and while the Overview's welcome panel stands in
 * for the coaching.
 */

import { useContext, useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { ActiveProjectContext } from '@/components/active-project-context'
import { MAIN_CONTENT_ID, SIDEBAR_ID } from '@/components/landmarks'
import { Button } from '@/components/ui/button'
import { useCanManageProject, useCanWriteProject } from '@/lib/permissions'
import { CoachBeacon } from './CoachBeacon'
import { clippedAxes, clippingAncestors, visibleFrame } from './coachGeometry'
import { DemoGuide } from './DemoGuide'
import {
  entryPresenceKey,
  useCoachPresence,
  useDemoScenario,
  useDemoScenarioActions,
} from './demoScenarioContext'
import { EndOfDemoLink } from './EndOfDemoLink'
import { CHAPTER_BLURBS, stepCompletedByPath } from './scenarioModel'
import {
  isOnStepPage,
  isOnStepSurface,
  stepPath,
  useDeferredFlag,
  useWelcomeStandsIn,
} from './stepLocation'

/**
 * How long a step goes without a mark before the host speaks. A route change
 * unmounts one surface's mark before the next surface mounts its own, and a
 * page's rows arrive after the page: a guide shown at once would flicker in
 * between.
 */
const GUIDE_DELAY_MS = 400

/**
 * How long the user must stand on the step's surface with no mark before the
 * guide says the control is missing — longer, since a slow page is the
 * likelier reason.
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

function hrefPath(link: Element): string {
  return (link.getAttribute('href') ?? '').split(/[?#]/)[0]?.replace(/\/$/, '') ?? ''
}

/** Laid out somewhere on the screen — a closed drawer keeps its items off it. */
function onScreen(element: Element): boolean {
  const rect = element.getBoundingClientRect()
  return rect.width > 0 && rect.right > 0 && rect.left < window.innerWidth
}

/**
 * The sidebar's link to the step's page, or to the section the page is in —
 * the longest link that leads there. None while the user is already in that
 * section: the ring would point where they stand.
 */
function sidebarLinkFor(path: string, pathname: string): HTMLElement | null {
  const sidebar = document.getElementById(SIDEBAR_ID)
  if (!sidebar) return null
  const target = path.replace(/\/$/, '')
  let best: HTMLElement | null = null
  let bestLength = 0
  for (const link of sidebar.querySelectorAll<HTMLElement>('a[href]')) {
    const href = hrefPath(link)
    if (href.length <= bestLength) continue
    if (target === href || target.startsWith(`${href}/`)) {
      best = link
      bestLength = href.length
    }
  }
  if (!best) return null
  const section = hrefPath(best)
  if (pathname === section || pathname.startsWith(`${section}/`)) return null
  // A closed drawer (a phone, a narrow window) keeps its items in the DOM,
  // off the screen: nothing there to point at.
  return onScreen(best) ? best : null
}

/**
 * The page's own tab that opens the step's section — Alerting's Rules for
 * `?section=monitors` — while the user is on the step's page at another one.
 * The sidebar has nothing to point at there (the user is on its item), and
 * "Open its page first" read as a riddle on the very page. Tabs carry their
 * value on the DOM (`ui/tabs`), matched rather than their label.
 */
function sectionTabFor(to: string, search: string): HTMLElement | null {
  const content = document.getElementById(MAIN_CONTENT_ID)
  if (!content) return null
  const current = new URLSearchParams(search)
  const wanted = [...new URLSearchParams(to.split('?')[1] ?? '')]
    .filter(([key, value]) => current.get(key) !== value)
    .map(([, value]) => value)
  if (wanted.length === 0) return null
  for (const tab of content.querySelectorAll<HTMLElement>('[role="tab"][data-tab-value]')) {
    if (wanted.includes(tab.dataset.tabValue ?? '') && onScreen(tab)) return tab
  }
  return null
}

/**
 * `sectionTabFor`, kept current while `to` is set: the tab strip renders with
 * the page's data, often after the guide has spoken.
 */
function useSectionTab(to: string | null, search: string): HTMLElement | null {
  const [tab, setTab] = useState<HTMLElement | null>(null)
  useEffect(() => {
    let frame = 0
    const find = () => {
      frame = 0
      setTab(to === null ? null : sectionTabFor(to, search))
    }
    const schedule = () => {
      if (frame === 0) frame = requestAnimationFrame(find)
    }
    schedule()
    const content = to === null ? null : document.getElementById(MAIN_CONTENT_ID)
    const observer =
      content && typeof MutationObserver !== 'undefined' ? new MutationObserver(schedule) : null
    if (content) observer?.observe(content, { childList: true, subtree: true })
    return () => {
      cancelAnimationFrame(frame)
      observer?.disconnect()
    }
  }, [to, search])
  return tab
}

export function DemoGuideHost() {
  const { active, state, activeChapter, chapters, nextChapter, step, steps, hintsMuted } =
    useDemoScenario()
  const { muteHints, unmuteHints, startChapter } = useDemoScenarioActions()
  const { present } = useCoachPresence()
  const location = useLocation()
  const { slug } = useParams()
  const welcomeStandsIn = useWelcomeStandsIn()
  const canEdit = useCanWriteProject()
  const canManage = useCanManageProject(useContext(ActiveProjectContext))

  const markPresent = present.has(step.id) || present.has(entryPresenceKey(step.id))
  const wanted =
    active && activeChapter !== null && !hintsMuted && !markPresent && !welcomeStandsIn
  const shown = useDeferredFlag(wanted, GUIDE_DELAY_MS)
  // Landed and not yet put away: the strip offers the next chapter, and so
  // does the guide, where the user is looking.
  const completed =
    !active && activeChapter !== null && state.chapters[activeChapter]?.status === 'completed'

  const path = stepPath(step.to)
  const onStepPage = isOnStepPage(location, step.to)
  const onSurface = isOnStepSurface(location, step.to)
  // A step with a control on a page, or one that completes by arriving
  // somewhere, has a page to send the user to. The rest — the search palette
  // step — happen wherever the user is.
  const hasPage =
    step.coach !== undefined ||
    (slug !== undefined && stepCompletedByPath(slug, step.id, path))
  // On the step's surface, yet no mark: the control is filtered out or not
  // rendered at all. A step that names a tab is on its surface only on that
  // tab. Steps without an on-surface control expect no mark.
  const targetMissing = useDeferredFlag(
    wanted && step.coach !== undefined && onSurface,
    MISSING_TARGET_DELAY_MS,
  )

  // The sidebar item that leads to the step's page. Found a frame after the
  // route settles, so the sidebar has rendered the route's own items by then,
  // and scrolled into the sidebar's view: a long sidebar scrolls, and Scans
  // sat below its fold, ringed where nobody could see it.
  const [sidebarLink, setSidebarLink] = useState<HTMLElement | null>(null)
  const pointSidebar = shown && hasPage && !onSurface
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const link = pointSidebar ? sidebarLinkFor(path, location.pathname) : null
      if (link && typeof link.scrollIntoView === 'function') {
        const frameBox = visibleFrame(clippingAncestors(link))
        if (clippedAxes(frameBox, link.getBoundingClientRect()).vertical) {
          link.scrollIntoView({ block: 'center' })
        }
      }
      setSidebarLink(link)
    })
    return () => cancelAnimationFrame(frame)
  }, [pointSidebar, path, location.pathname])

  // On the step's page at another of its sections: the way there is a tab.
  const onStepPath = location.pathname.replace(/\/$/, '') === path.replace(/\/$/, '')
  const sectionTab = useSectionTab(
    shown && step.coach !== undefined && onStepPath && !onSurface ? step.to : null,
    location.search,
  )

  if (welcomeStandsIn || !activeChapter) return null

  const position = steps.findIndex((candidate) => candidate.id === step.id) + 1
  const chapterTitle = chapters.find((chapter) => chapter.id === activeChapter)?.title

  // Hints hidden: the face alone, in its corner, and a click on it brings
  // them back. The strip's "Show hints" sat above the page, scrolled away.
  if (hintsMuted && (active || completed)) {
    return (
      <DemoGuide
        key="muted"
        stepKey={step.id}
        position={position}
        total={steps.length}
        chapter={chapterTitle}
        complete={completed}
        title={completed ? (chapterTitle ?? '') : step.title}
        instruction={completed ? '' : step.instruction}
        onUnmute={unmuteHints}
      />
    )
  }

  if (completed) {
    const stepKey = `${activeChapter}@complete`
    return (
      <DemoGuide
        key={stepKey}
        stepKey={stepKey}
        position={steps.length}
        total={steps.length}
        chapter={chapterTitle}
        complete
        title={nextChapter ? `Next: ${nextChapter.title}` : 'You have walked the whole product'}
        instruction={
          nextChapter ? CHAPTER_BLURBS[nextChapter.id] : 'Point it at your own warehouse next.'
        }
        action={
          nextChapter ? (
            <Button asChild size="xs">
              {/* Started on click, before the Link navigates, so the user
                  lands on the chapter's surface with its first step live. */}
              <Link to={nextChapter.to} onClick={() => startChapter(nextChapter.id)}>
                Start the chapter
                <ArrowRight className="size-3" aria-hidden="true" />
              </Link>
            </Button>
          ) : (
            <EndOfDemoLink />
          )
        }
        onMute={muteHints}
      />
    )
  }

  if (!shown) return null

  // Off the step's page, the first gesture is getting there; a visit step's
  // own gesture already is ("Click Properties in the sidebar").
  const cue =
    step.coach !== undefined && !onSurface
      ? sectionTab
        ? 'Open the highlighted tab first, or click Take me there.'
        : sidebarLink && pointSidebar
          ? 'Open its page first — the highlighted sidebar item, or Take me there.'
          : 'Open its page first: click Take me there.'
      : step.cue
  const note = targetMissing
    ? !canEdit
      ? NEEDS_EDITOR_COPY
      : canManage
        ? `${MISSING_TARGET_COPY} ${RESET_RESTORES_COPY}`
        : MISSING_TARGET_COPY
    : undefined

  return (
    <>
      {sidebarLink && pointSidebar && (
        <CoachBeacon anchor={sidebarLink} side="right" align="center" />
      )}
      {sectionTab && (
        <CoachBeacon anchor={sectionTab} tag="Open this tab" side="bottom" align="center" />
      )}
      <DemoGuide
        // Folding is a choice about one step: the next one opens unfolded.
        key={step.id}
        stepKey={step.id}
        position={position}
        total={steps.length}
        chapter={chapterTitle}
        title={step.title}
        instruction={step.instruction}
        cue={cue}
        note={note}
        avoid={sectionTab}
        action={
          hasPage && !onStepPage ? (
            <Button asChild size="xs">
              <Link to={step.to}>
                Take me there
                <ArrowRight className="size-3" aria-hidden="true" />
              </Link>
            </Button>
          ) : undefined
        }
        onMute={muteHints}
      />
    </>
  )
}
