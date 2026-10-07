/**
 * The demo guide whenever no coach mark is speaking.
 *
 * A mark on the step's control shows the guide itself, beside its ring. The
 * rest of a running chapter used to have nothing on the page: a step whose
 * control is on another page, a step that is only a visit ("Click Coverage in
 * the sidebar"), a control filtered out of view. Visitors stood there not
 * knowing where to click. The host fills those moments with the same guide —
 * what the step is, the gesture, the way there (a link to the step's page and
 * a ring on its item in the sidebar) — and, on the page with the control
 * nowhere to be seen, why.
 *
 * Mounted by the shell beside the demo banner, for demo projects. It renders
 * nothing while a mark for the step is on screen (its own, or the way back to
 * it), while hints are muted, while no chapter runs, and while the Overview's
 * welcome panel stands in for the coaching.
 */

import { useContext, useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { ActiveProjectContext } from '@/components/active-project-context'
import { SIDEBAR_ID } from '@/components/landmarks'
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
import { stepCompletedByPath } from './scenarioModel'
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
  const rect = best.getBoundingClientRect()
  return rect.width > 0 && rect.right > 0 && rect.left < window.innerWidth ? best : null
}

export function DemoGuideHost() {
  const { active, activeChapter, chapters, step, steps, hintsMuted } = useDemoScenario()
  const { muteHints } = useDemoScenarioActions()
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

  if (!shown || !activeChapter) return null

  const position = steps.findIndex((candidate) => candidate.id === step.id) + 1
  const chapterTitle = chapters.find((chapter) => chapter.id === activeChapter)?.title
  // Off the step's page, the first gesture is getting there; a visit step's
  // own gesture already is ("Click Properties in the sidebar").
  const cue =
    step.coach !== undefined && !onSurface
      ? sidebarLink && pointSidebar
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
