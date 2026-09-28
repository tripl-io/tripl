/**
 * Project event stream subscription hook (tripl-2su6.8).
 *
 * Opens an `EventSource` to `GET /api/v1/projects/{slug}/events/stream` and, on
 * each named event, invalidates the mapped React-Query keys (see
 * {@link ./invalidationMap}). One-way: server → client only.
 *
 * Reconnect: manual with jittered exponential backoff, carrying the
 * `Last-Event-ID` cursor as `?last_event_id=` so the server can replay missed
 * events. Coming back online or to a visible tab reconnects at once instead of
 * waiting out the timer. Duplicate delivery (replay, StrictMode remount) is
 * harmless — invalidation is idempotent — and additionally de-duplicated by
 * monotonic event id.
 *
 * Resync: the server's replay ring is small, and a Redis restart without
 * persistence starts the per-project sequence again at 1. The `hello` event
 * carries the project's current sequence number (`seq`) and the ring's size, so
 * after a reconnect the client compares `seq` with its cursor: a gap the ring
 * covers, in the same sequence `epoch`, arrives as replay and needs nothing
 * more (tripl-fj5g.17) — as long as the replayed ids arrive without a hole,
 * which is checked as they come in. Only when it cannot tell — no `seq`, no
 * cursor, a new epoch — or the gap is wider than the ring, or `seq` fell below
 * the cursor (a reset), or the replay has a hole, does it refresh every project cache the
 * stream feeds, once, and forget the de-dupe high-water mark. An id far below
 * that mark is read as a sequence reset rather than as a stale duplicate.
 * Without this, a reset left every surface silently stale while the status
 * still read `live`, which also switches polling off.
 *
 * The cursor starts at the first `hello`'s `seq`, not at the first event: the
 * page's own fetches cover everything before it, and a client that has seen no
 * event yet can still be replayed what it missed while disconnected.
 *
 * Status: `connecting` until the server's `hello` event, then `live` (Redis pub/
 * sub delivering) or `degraded` (Redis off — clients keep polling); `closed`
 * while disconnected/reconnecting. The returned status drives the adaptive
 * polling fallback. Status transitions happen only from async callbacks (SSE
 * events, reconnect timers) or a render-time reset on slug change — never
 * synchronously inside the effect body.
 */

import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { StreamStatus } from './pollingPolicy'
import { PROJECT_EVENT_TYPES, invalidateForEvent, isProjectEventType } from './invalidationMap'
import { orgScopedPath } from '@/api/client'
import { useActiveOrg } from '@/components/active-org-context'

const BASE_BACKOFF_MS = 1000
const MAX_BACKOFF_MS = 30_000
/** ±30%, so clients dropped together by a deploy do not reconnect in lockstep. */
const BACKOFF_JITTER = 0.3
const HELLO_EVENT = 'hello'
/**
 * An id this far below the last one processed cannot be a replay — the
 * server's replay ring (backend/src/tripl/realtime.py `BUFFER_SIZE`) holds 50 —
 * so the sequence was reset.
 */
const SEQUENCE_RESET_GAP = 50

/** Backoff for the given attempt: exponential, capped, jittered. */
export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS)
  return Math.round(base * (1 - BACKOFF_JITTER + random() * 2 * BACKOFF_JITTER))
}

interface HelloPayload {
  backend?: string
  /** The project's sequence number when the stream opened; null when unknown. */
  seq?: number | null
  /** Identity of that sequence: a new one means Redis lost the counter. */
  epoch?: string | null
  /** How many events the server's replay ring holds. */
  buffer_size?: number
}

export interface Hello {
  backend: string | undefined
  seq: number | null
  epoch: string | null
  bufferSize: number
}

function parseHello(data: string): Hello {
  let payload: HelloPayload = {}
  try {
    const parsed: unknown = JSON.parse(data)
    if (parsed && typeof parsed === 'object') payload = parsed as HelloPayload
  } catch {
    // An unreadable hello is a hello that cannot tell what was missed.
  }
  const seq = typeof payload.seq === 'number' && Number.isInteger(payload.seq) && payload.seq >= 0
    ? payload.seq
    : null
  const bufferSize = typeof payload.buffer_size === 'number' && payload.buffer_size > 0
    ? payload.buffer_size
    : SEQUENCE_RESET_GAP
  const epoch = typeof payload.epoch === 'string' && payload.epoch ? payload.epoch : null
  return { backend: payload.backend, seq, epoch, bufferSize }
}

/**
 * After a reconnect, whether the server's replay (events past `cursor`, sent
 * right after `hello`) can cover everything the client missed. `false` means
 * the client cannot know, and must refetch. `epoch` is the sequence identity the
 * cursor belongs to: a Redis restart without persistence starts a new epoch,
 * and its restarted ids can climb back past the cursor while the client sleeps.
 * A `true` is still checked as the replay arrives (see the stream hook).
 */
export function replayCoversGap(hello: Hello, cursor: string | null, epoch: string | null): boolean {
  if (hello.backend !== 'redis' || hello.seq === null || cursor === null) return false
  if (epoch === null || hello.epoch !== epoch) return false
  const last = Number(cursor)
  if (!Number.isInteger(last) || last < 0) return false
  // Below the cursor: the sequence restarted, and ids the client has already
  // seen now name different events.
  if (hello.seq < last) return false
  return hello.seq - last <= hello.bufferSize
}

function initialStatus(slug: string | undefined): StreamStatus {
  return slug ? 'connecting' : 'closed'
}

function streamUrl(slug: string, lastEventId: string | null): string {
  // Inside the active organization, like every request `api` sends (F20 PR7).
  const base = `/api/v1${orgScopedPath(`/projects/${encodeURIComponent(slug)}/events/stream`)}`
  return lastEventId ? `${base}?last_event_id=${encodeURIComponent(lastEventId)}` : base
}

/**
 * Subscribe to a project's live update stream. Returns the current stream status
 * (also used by the adaptive polling policy). No-op when `slug` is undefined.
 */
export function useProjectEventStream(slug: string | undefined): StreamStatus {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<StreamStatus>(() => initialStatus(slug))

  // Reset status the moment the project scope changes (render-time derived state,
  // the React-documented alternative to a setState-in-effect). The connection
  // effect below then re-subscribes for the new slug.
  // The organization is part of the scope (F20 PR7): two organizations may each
  // hold a project `web`, and switching between them is a new stream.
  const { slug: org } = useActiveOrg()
  const scope = `${org ?? ''}/${slug ?? ''}`
  const [lastScope, setLastScope] = useState(scope)
  if (lastScope !== scope) {
    setLastScope(scope)
    setStatus(initialStatus(slug))
  }

  // Mutable connection state kept in refs so reconnect scheduling never triggers
  // re-subscribes; the effect re-runs only when the project slug changes.
  const sourceRef = useRef<EventSource | null>(null)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const attemptsRef = useRef(0)
  const lastEventIdRef = useRef<string | null>(null)
  const lastProcessedIdRef = useRef(0)
  // A `hello` was seen on this slug's stream before, so the next one follows a
  // reconnect and has to resync.
  const helloSeenRef = useRef(false)
  // The sequence identity the cursor belongs to (the last hello's `epoch`).
  const epochRef = useRef<string | null>(null)
  // A replay the last hello promised: ids `next`..`until`, in order, before any
  // live event. Null when nothing is owed.
  const pendingReplayRef = useRef<{ next: number; until: number } | null>(null)

  useEffect(() => {
    // Redis sequences are scoped per project. Carrying a previous project's
    // cursor into the new stream would make its low-numbered events look like
    // duplicates and can strand query caches until a full page reload.
    attemptsRef.current = 0
    lastEventIdRef.current = null
    lastProcessedIdRef.current = 0
    helloSeenRef.current = false
    epochRef.current = null
    pendingReplayRef.current = null
    if (!slug) return

    let disposed = false

    const clearReconnect = () => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
    }

    const scheduleReconnect = () => {
      if (disposed) return
      const delay = reconnectDelay(attemptsRef.current)
      attemptsRef.current += 1
      clearReconnect()
      reconnectTimerRef.current = setTimeout(connect, delay)
    }

    const resyncAll = () => {
      pendingReplayRef.current = null
      for (const type of PROJECT_EVENT_TYPES) invalidateForEvent(queryClient, type, slug)
    }

    const handleProjectEvent = (event: MessageEvent) => {
      if (disposed) return
      if (event.lastEventId) lastEventIdRef.current = event.lastEventId
      // De-dupe by monotonic id so a reconnect replay never double-invalidates.
      const id = Number(event.lastEventId)
      const pending = pendingReplayRef.current
      if (pending && Number.isInteger(id)) {
        // The replay the hello promised must arrive whole and in order. A hole —
        // an event the ring never kept (a publish whose buffer write failed) —
        // or a live event overtaking it means something was missed: refetch.
        if (id === pending.next) {
          pending.next += 1
          if (pending.next > pending.until) pendingReplayRef.current = null
        } else if (id > pending.next) {
          resyncAll()
        }
      }
      if (Number.isFinite(id) && id > 0) {
        const last = lastProcessedIdRef.current
        const sequenceReset = last - id > SEQUENCE_RESET_GAP
        if (id <= last && !sequenceReset) return
        lastProcessedIdRef.current = id
      }
      if (isProjectEventType(event.type)) {
        invalidateForEvent(queryClient, event.type, slug)
      }
    }

    const handleHello = (event: MessageEvent) => {
      if (disposed) return
      attemptsRef.current = 0
      const hello = parseHello(String(event.data))
      setStatus(hello.backend === 'redis' ? 'live' : 'degraded')
      const reconnected = helloSeenRef.current
      helloSeenRef.current = true
      pendingReplayRef.current = null
      const cursor = lastEventIdRef.current
      const covered = reconnected && replayCoversGap(hello, cursor, epochRef.current)
      epochRef.current = hello.seq === null ? null : hello.epoch
      // Back after a disconnect with a gap the replay ring covers: the missed
      // events follow this one, and the cursor advances with them — provided
      // they all arrive, which handleProjectEvent checks.
      if (covered) {
        const from = Number(cursor) + 1
        if (hello.seq !== null && from <= hello.seq) {
          pendingReplayRef.current = { next: from, until: hello.seq }
        }
        return
      }
      if (reconnected) {
        // Cannot tell what was missed, or missed more than the ring holds:
        // refetch everything, and let events numbered from a restarted
        // sequence count again.
        lastProcessedIdRef.current = 0
        resyncAll()
      }
      // The page's fetches (or the refetch above) cover everything up to `seq`,
      // so it is where the next reconnect's replay starts.
      if (hello.seq !== null) {
        lastEventIdRef.current = String(hello.seq)
        lastProcessedIdRef.current = hello.seq
      }
    }

    function connect() {
      if (disposed) return
      const EventSourceCtor = globalThis.EventSource
      if (!EventSourceCtor) return
      const source = new EventSourceCtor(streamUrl(slug!, lastEventIdRef.current), {
        withCredentials: true,
      })
      sourceRef.current = source

      source.addEventListener(HELLO_EVENT, handleHello as EventListener)
      for (const type of PROJECT_EVENT_TYPES) {
        source.addEventListener(type, handleProjectEvent as EventListener)
      }
      source.onerror = () => {
        // EventSource auto-reconnect is replaced with our own backed-off retry so
        // the cursor is carried and the status reflects the disconnect.
        source.close()
        if (sourceRef.current === source) sourceRef.current = null
        if (disposed) return
        setStatus('closed')
        scheduleReconnect()
      }
    }

    // Waiting out a backoff timer after the laptop wakes or the network returns
    // could take 30 s; the browser says so directly, so reconnect right away.
    const reconnectNow = () => {
      if (disposed || sourceRef.current !== null) return
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      clearReconnect()
      attemptsRef.current = 0
      connect()
    }
    window.addEventListener('online', reconnectNow)
    document.addEventListener('visibilitychange', reconnectNow)

    connect()

    return () => {
      disposed = true
      window.removeEventListener('online', reconnectNow)
      document.removeEventListener('visibilitychange', reconnectNow)
      clearReconnect()
      if (sourceRef.current !== null) {
        sourceRef.current.close()
        sourceRef.current = null
      }
    }
  }, [slug, org, queryClient])

  return status
}
