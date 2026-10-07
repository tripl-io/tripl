/**
 * The demo guide: a friendly face in a corner of the page that says what the
 * current step is and exactly what to do, while the ring on the page shows
 * where.
 *
 * It replaces the coach card that opened beside its control. Beside a control
 * there is always something else, and visitors found the card covering text —
 * and buttons covering the card. The guide takes a corner of the content
 * column instead, the first one that keeps clear of the control it is
 * coaching (`guidePlacement`), and moves when the control moves.
 *
 * A hint, never a dialog: it takes no focus, traps nothing, and Escape or a
 * click elsewhere cannot dismiss it. It minimises to its face, and "Hide
 * hints" quiets the coaching for the session, as before.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Minus, MousePointerClick } from 'lucide-react'
import { MAIN_CONTENT_ID } from '@/components/landmarks'
import { GuideMascot } from './GuideMascot'
import {
  CORNER_ORDER,
  chooseCorner,
  cornerBox,
  coveredArea,
  pickCorner,
  type Box,
  type GuideCorner,
  type GuideFrame,
  type WeightedBox,
} from './guidePlacement'

/** The guide's width from `sm` up; below it the guide spans the screen. */
const GUIDE_WIDTH_PX = 336
/** Before the guide has been measured. */
const ESTIMATED_HEIGHT_PX = 176
/** The folded guide: its face, in a round button. */
const FACE_SIZE_PX = 56
/** Below `sm`: a phone, where the guide is a bottom sheet. */
const PHONE_QUERY = '(max-width: 639px)'
/** On a phone the guide spans the width, so only top or bottom is a choice. */
const PHONE_CORNERS: readonly GuideCorner[] = ['bottom-left', 'top-left']
/** Clear of the 44px top bar. */
const TOP_BAR_CLEARANCE_PX = 56
const EDGE_GAP_PX = 16
const PHONE_GAP_PX = 12
/** Set on the root while the guide holds a bottom corner; Layout pads the column by it. */
const CLEARANCE_VAR = '--demo-guide-clearance'

function isPhone(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(PHONE_QUERY).matches
}

/**
 * Where a guide in a top corner may start: under the top bar, and under the
 * demo banner while that is on screen — the banner holds the controls that put
 * the coaching away (hide, dismiss, the tour), which a guide must not cover.
 * Never past the upper third.
 */
function topClearance(): number {
  const banner = document.querySelector('[data-demo-banner]')
  if (!banner) return TOP_BAR_CLEARANCE_PX
  const below = banner.getBoundingClientRect().bottom + 8
  return Math.round(Math.max(TOP_BAR_CLEARANCE_PX, Math.min(below, window.innerHeight / 3)))
}

/** The content column — the guide never sits on the sidebar or the activity rail. */
function guideFrame(phone: boolean): GuideFrame {
  const width = window.innerWidth
  const bottom = window.innerHeight - (phone ? PHONE_GAP_PX : EDGE_GAP_PX)
  if (phone) return { left: PHONE_GAP_PX, right: width - PHONE_GAP_PX, top: topClearance(), bottom }
  const column = document.getElementById(MAIN_CONTENT_ID)?.getBoundingClientRect()
  const left = Math.max(EDGE_GAP_PX, column ? column.left : EDGE_GAP_PX)
  const right = Math.min(
    width - EDGE_GAP_PX,
    column && column.width > 0 ? column.right : width - EDGE_GAP_PX,
  )
  return { left, right: Math.max(right, left), top: topClearance(), bottom }
}

/** The open card's width: on a phone, the screen's less a gutter each side. */
function cardWidth(phone: boolean): number {
  return phone ? window.innerWidth - 2 * PHONE_GAP_PX : GUIDE_WIDTH_PX
}

function sameBox(a: Box, b: Box): boolean {
  return a.top === b.top && a.left === b.left && a.right === b.right && a.bottom === b.bottom
}

/** A visible box for `element`, or null when it has none to keep clear of. */
function visibleBox(element: Element | null | undefined): Box | null {
  if (!element) return null
  const rect = element.getBoundingClientRect()
  if (rect.width === 0 && rect.height === 0) return null
  return { top: rect.top, left: rect.left, right: rect.right, bottom: rect.bottom }
}

/**
 * Open dialogs, menus, popovers and listboxes. The guide sits above them — a
 * step's control may live in one — so it must not sit ON them: a menu that
 * opened down into the guide's corner had its items covered.
 */
const FLOATING_LAYERS = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]'

function floatingLayerBoxes(self: Element | null): Box[] {
  const boxes: Box[] = []
  for (const layer of document.querySelectorAll(FLOATING_LAYERS)) {
    if (self?.contains(layer)) continue
    const box = visibleBox(layer)
    if (box) boxes.push(box)
  }
  return boxes
}

/** The page's own controls — worse to cover than its text. */
const PAGE_CONTROLS =
  'a[href], button, input, select, textarea, [role="button"], [role="tab"], [role="checkbox"], [role="switch"]'
const PAGE_TEXT = 'h1, h2, h3, h4, p, label, li, td, th'
const CONTROL_WEIGHT = 3

/**
 * What of the page is on screen to keep the card off, so it takes the
 * emptiest corner — the testers found it on a "Merge" button while the
 * other side of the page was bare.
 */
function busyBoxes(self: Element | null): WeightedBox[] {
  const root = document.getElementById(MAIN_CONTENT_ID)
  if (!root) return []
  const boxes: WeightedBox[] = []
  const collect = (selector: string, weight: number) => {
    for (const element of root.querySelectorAll(selector)) {
      if (self?.contains(element)) continue
      const rect = element.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) continue
      if (rect.bottom < 0 || rect.top > window.innerHeight) continue
      boxes.push({
        box: { top: rect.top, left: rect.left, right: rect.right, bottom: rect.bottom },
        weight,
      })
    }
  }
  collect(PAGE_CONTROLS, CONTROL_WEIGHT)
  collect(PAGE_TEXT, 1)
  return boxes
}

interface Layout {
  corner: GuideCorner
  box: Box
}

/**
 * The minimised step, for the browser session: minimising is a choice about
 * this step, so the guide stays small across the pages the step spans and
 * opens again for the next one.
 */
const MINIMISED_KEY = 'tripl-demo-guide-minimised'

function readMinimised(stepKey: string): boolean {
  try {
    return window.sessionStorage.getItem(MINIMISED_KEY) === stepKey
  } catch {
    return false
  }
}

function writeMinimised(stepKey: string, minimised: boolean): void {
  try {
    if (minimised) window.sessionStorage.setItem(MINIMISED_KEY, stepKey)
    else window.sessionStorage.removeItem(MINIMISED_KEY)
  } catch {
    /* ignore — the guide still minimises on this page */
  }
}

export interface DemoGuideProps {
  /** Which step this is, for remembering that it was minimised. */
  stepKey: string
  position: number
  total: number
  /** The chapter, printed beside the step count. */
  chapter?: string
  title: string
  instruction: string
  /** Set when a control elsewhere is described by the instruction. */
  instructionId?: string
  /** The exact gesture ("Click Run now."). */
  cue?: string
  /** The control the ring is on: the guide keeps clear of it. */
  avoid?: HTMLElement | null
  /** The guide's way to the step's surface, when the user is not on it. */
  action?: ReactNode
  /** Said when the step's control cannot be pointed at here. */
  note?: string
  onMute?: () => void
}

export function DemoGuide({
  stepKey,
  position,
  total,
  chapter,
  title,
  instruction,
  instructionId,
  cue,
  avoid,
  action,
  note,
  onMute,
}: DemoGuideProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [minimised, setMinimised] = useState(() => readMinimised(stepKey))
  // No corner left for the card — a dialog as big as the screen — so the
  // guide steps aside to its face, unless the user opened it anyway.
  const [squeezed, setSqueezed] = useState(false)
  const [openAnyway, setOpenAnyway] = useState(false)
  const [phone, setPhone] = useState(isPhone)
  const [layout, setLayout] = useState<Layout | null>(null)
  // The open card's height, kept while it is folded: it decides whether the
  // card would fit again.
  const cardHeight = useRef(ESTIMATED_HEIGHT_PX)
  const heldCorner = useRef<GuideCorner | null>(null)
  const small = minimised || (squeezed && !openAnyway)

  const measure = useCallback(() => {
    const onPhone = isPhone()
    setPhone(onPhone)
    const frame = guideFrame(onPhone)
    const element = ref.current
    if (element && element.dataset.minimised !== 'true' && element.offsetHeight > 0) {
      cardHeight.current = element.offsetHeight
    }
    const target = visibleBox(avoid)
    const avoidBoxes = [...(target ? [target] : []), ...floatingLayerBoxes(element)]
    const order = onPhone ? PHONE_CORNERS : CORNER_ORDER
    const card = { width: cardWidth(onPhone), height: cardHeight.current }
    // The card keeps its corner while that stays clear: a guide that hopped
    // to the emptiest corner on every scroll would be chased round the screen.
    const held = heldCorner.current
    const cardAt =
      held && order.includes(held) && coveredArea(cornerBox(held, frame, card), avoidBoxes) === 0
        ? held
        : pickCorner(frame, card, avoidBoxes, busyBoxes(element), order)
    heldCorner.current = cardAt
    const tight = cardAt === null
    setSqueezed(tight)
    const folded = minimised || (tight && !openAnyway)
    const size = folded ? { width: FACE_SIZE_PX, height: FACE_SIZE_PX } : card
    const corner = folded
      ? chooseCorner(frame, size, avoidBoxes, order)
      : (cardAt ?? chooseCorner(frame, card, avoidBoxes, order))
    const box = cornerBox(corner, frame, size)
    setLayout((prev) =>
      prev && prev.corner === corner && sameBox(prev.box, box) ? prev : { corner, box },
    )
  }, [avoid, minimised, openAnyway])

  // Placed before paint, so it never shows in a corner it is about to leave.
  useLayoutEffect(() => {
    const place = () => measure()
    place()
  }, [measure, title, instruction, cue, note])

  // The control moves without telling anyone — a row is inserted above it, a
  // section expands, the page scrolls — so the guide re-measures on every
  // layout change, coalesced to one measurement a frame.
  useEffect(() => {
    let frame = 0
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
    }
    window.addEventListener('resize', schedule)
    window.addEventListener('scroll', schedule, { capture: true, passive: true })
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    if (ref.current) resize?.observe(ref.current)
    // Attributes too: a menu mounts first and is positioned after, by its style.
    const mutations =
      typeof MutationObserver === 'undefined' ? null : new MutationObserver(schedule)
    mutations?.observe(document.body, { childList: true, subtree: true, attributes: true })
    // A dialog or menu that animates in ends somewhere new.
    window.addEventListener('transitionend', schedule, true)
    window.addEventListener('animationend', schedule, true)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', schedule)
      window.removeEventListener('scroll', schedule, { capture: true })
      window.removeEventListener('transitionend', schedule, true)
      window.removeEventListener('animationend', schedule, true)
      resize?.disconnect()
      mutations?.disconnect()
    }
  }, [measure])

  // Room under the page for the guide: while it sits in a bottom corner, the
  // content column ends with as much space as the guide takes from the screen
  // (Layout's `<main>`), so a page's last rows can always be scrolled out
  // from under it.
  useEffect(() => {
    if (!layout || !layout.corner.startsWith('bottom')) return
    const root = document.documentElement
    root.style.setProperty(CLEARANCE_VAR, `${Math.ceil(window.innerHeight - layout.box.top)}px`)
    return () => {
      root.style.removeProperty(CLEARANCE_VAR)
    }
  }, [layout])

  const fold = () => {
    setMinimised(true)
    setOpenAnyway(false)
    writeMinimised(stepKey, true)
  }
  const unfold = () => {
    if (minimised) {
      setMinimised(false)
      writeMinimised(stepKey, false)
    }
    // Folded only for want of room: the user asked for it all the same.
    if (squeezed) setOpenAnyway(true)
  }

  const box = layout?.box
  return createPortal(
    <div
      ref={ref}
      role="note"
      aria-label="Demo hint"
      data-demo-guide=""
      data-guide-corner={layout?.corner}
      data-minimised={small ? 'true' : undefined}
      // Above dialogs: a step whose control is inside one stays coached. The
      // shared Dialog ignores clicks on the guide, so using it never closes
      // the dialog it is coaching.
      className="fixed z-(--z-guide) motion-safe:transition-[top,left] motion-safe:duration-200"
      style={{
        top: box?.top ?? 0,
        left: box?.left ?? 0,
        // The card's own width, never its last box's: just unfolded, that box
        // is still the face's, and a card measured that narrow is too tall for
        // any corner, so it folded straight back.
        width: small ? undefined : cardWidth(phone),
        // A Radix modal turns pointer events off on <body>; the guide lives in
        // <body>, so it turns them back on for itself.
        pointerEvents: 'auto',
        // Unmeasured, it would paint at the corner of the screen for a frame.
        visibility: layout ? 'visible' : 'hidden',
      }}
    >
      {small ? (
        <button
          type="button"
          onClick={(event) => {
            // A portal still bubbles through the React tree: this click must
            // not reach the row the coached control sits in.
            event.stopPropagation()
            unfold()
          }}
          aria-expanded={false}
          aria-label={`Show the demo guide — step ${position} of ${total}: ${title}`}
          className="relative flex size-14 items-center justify-center rounded-full border shadow-lg transition-colors hover:bg-[var(--surface-hover)] bg-bg-elevated"
          style={{ borderColor: 'var(--accent)' }}
        >
          <GuideMascot size={38} />
          <span
            aria-hidden="true"
            className="absolute -top-1 -right-1 rounded-full px-1.5 py-px text-caption font-semibold tabular-nums bg-accent-solid text-accent-solid-fg"
          >
            {position}/{total}
          </span>
          {/* Kept for the control the instruction describes. */}
          <span id={instructionId} className="sr-only">
            {instruction}
          </span>
        </button>
      ) : (
        <div
          className="relative flex items-start gap-3 rounded-xl border p-3 pr-9 text-left shadow-lg bg-bg-elevated"
          style={{ borderColor: 'var(--accent)' }}
        >
          <div className="pt-0.5">
            <GuideMascot size={44} />
          </div>
          <div className="min-w-0 flex-1 space-y-1.5">
            <p className="micro-label text-fg-tertiary">
              <span>
                Step {position} of {total}
              </span>
              {chapter && <span className="normal-case tracking-normal"> · {chapter}</span>}
            </p>
            <p className="text-body-sm font-semibold text-fg">{title}</p>
            <p id={instructionId} className="text-body-sm leading-[1.5] text-fg-secondary">
              {instruction}
            </p>
            {cue && (
              <p className="flex items-start gap-1.5 rounded-md px-2 py-1.5 text-body-sm font-medium leading-[1.45] bg-accent-soft text-fg">
                <MousePointerClick
                  className="mt-0.5 size-3.5 shrink-0 text-accent"
                  aria-hidden="true"
                />
                <span>{cue}</span>
              </p>
            )}
            {note && <p className="text-caption leading-[1.45] text-fg-secondary">{note}</p>}
            {(action || onMute) && (
              <div className="flex flex-wrap items-center gap-2 pt-0.5">
                {action}
                {onMute && (
                  <button
                    type="button"
                    onClick={(event) => {
                      // Portalled beside its control: not a click on its row.
                      event.stopPropagation()
                      onMute()
                    }}
                    className="rounded-sm px-1.5 py-0.5 text-caption font-medium transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
                  >
                    Hide hints
                  </button>
                )}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              fold()
            }}
            aria-expanded
            aria-label="Minimise the demo guide"
            title="Minimise"
            className="absolute right-1.5 top-1.5 flex size-7 items-center justify-center rounded-sm transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
          >
            <Minus className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      )}
    </div>,
    document.body,
  )
}
