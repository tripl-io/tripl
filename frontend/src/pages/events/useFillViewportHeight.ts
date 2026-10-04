import { useLayoutEffect, useState, type RefObject } from 'react'

/** The nearest ancestor that scrolls vertically, or null for the window. */
function scrollParentOf(el: HTMLElement): HTMLElement | null {
  let node = el.parentElement
  while (node) {
    const { overflowY } = getComputedStyle(node)
    if (overflowY === 'auto' || overflowY === 'scroll') return node
    node = node.parentElement
  }
  return null
}

/**
 * The height that makes `ref`'s element end at the bottom of the viewport when
 * the page is scrolled to the top, less `reserve` (whatever sits under it).
 *
 * The events table used to size its scroller with magic offsets
 * (`calc(100vh - 455px)` / `285px`), which assumed one header, one toolbar row
 * and the chart: with the demo banner and a wrapped toolbar it came out ~300px
 * tall, about nine rows at 1440×900. Measuring where the scroller
 * actually starts fills the rest of the screen whatever sits above it.
 *
 * Re-measured on resize and whenever `observe`'s element changes size (the
 * page root: a toolbar that wraps, a chart that opens). The result never
 * feeds back: the scroller's own height does not move its top.
 */
export function useFillViewportHeight(
  ref: RefObject<HTMLElement | null>,
  { reserve, min, observe }: { reserve: number; min: number; observe?: string },
): number | null {
  const [height, setHeight] = useState<number | null>(null)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof window === 'undefined') return

    const measure = () => {
      const parent = scrollParentOf(el)
      const viewportTop = parent ? parent.getBoundingClientRect().top : 0
      const viewportHeight = parent ? parent.clientHeight : window.innerHeight
      const scrollTop = parent ? parent.scrollTop : window.scrollY
      // Where the scroller starts with the page scrolled to the top.
      const top = el.getBoundingClientRect().top - viewportTop + scrollTop
      const next = Math.max(min, Math.round(viewportHeight - top - reserve))
      setHeight(prev => (prev === next ? prev : next))
    }

    measure()
    window.addEventListener('resize', measure)
    const root = observe ? el.closest(observe) : null
    const observer =
      root && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    if (root) observer?.observe(root)
    return () => {
      window.removeEventListener('resize', measure)
      observer?.disconnect()
    }
  }, [ref, reserve, min, observe])

  return height
}
