/**
 * Mounts the project event stream once per project scope and publishes its
 * status to descendants. Placed high in the app Layout so the top
 * bar, activity rail, and routed pages all read one shared stream status.
 */

import type { ReactNode } from 'react'
import { StreamStatusContext } from './streamContext'
import { useProjectEventStream } from './useProjectEventStream'

export function ProjectEventStreamProvider({
  slug,
  children,
}: {
  slug?: string
  children: ReactNode
}) {
  const status = useProjectEventStream(slug)
  return <StreamStatusContext.Provider value={status}>{children}</StreamStatusContext.Provider>
}
