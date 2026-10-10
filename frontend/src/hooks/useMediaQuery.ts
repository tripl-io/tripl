import { useCallback, useSyncExternalStore } from 'react'

function hasMatchMedia(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
}

/**
 * Whether a CSS media query matches, kept live as it changes.
 *
 * Where there is no `matchMedia` (and on the server) it answers `fallback`,
 * which the caller picks as the safe guess for its own layout: a sidebar that
 * must never be made inert unmeasured says `true`, a rail that must never block
 * the content column says `false`. `window.matchMedia` is read on every call,
 * not captured, so a test that replaces it is what the hook sees.
 */
export function useMediaQuery(query: string, fallback = false): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (!hasMatchMedia()) return () => {}
      const list = window.matchMedia(query)
      list.addEventListener('change', onChange)
      return () => list.removeEventListener('change', onChange)
    },
    [query],
  )
  const read = () => (hasMatchMedia() ? window.matchMedia(query).matches : fallback)
  return useSyncExternalStore(subscribe, read, () => fallback)
}

/** A touch screen: the finger covers what a hover tooltip sits beside. */
export const COARSE_POINTER_QUERY = '(pointer: coarse)'
