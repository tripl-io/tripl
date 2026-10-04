import { useEffect, useState } from 'react'

/**
 * The current time, re-read every `intervalMs`. For relative timestamps
 * ("5 min ago") that must keep counting while nothing else re-renders — with
 * the live stream up the activity rail never polls, and "just now" used to stay
 * "just now" for hours.
 */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}
