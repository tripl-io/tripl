import type { ReactNode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reconnectDelay, replayCoversGap, useProjectEventStream } from './useProjectEventStream'
import { at } from '@/test/at'

// Minimal EventSource stand-in: records instances + listeners so tests can drive
// events and inspect reconnect behaviour (jsdom ships no EventSource).
class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  withCredentials: boolean
  onerror: ((e: Event) => void) | null = null
  closed = false
  private listeners: Record<string, Array<(e: MessageEvent) => void>> = {}

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url
    this.withCredentials = init?.withCredentials ?? false
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, cb: (e: MessageEvent) => void): void {
    ;(this.listeners[type] ??= []).push(cb)
  }

  removeEventListener(): void {}

  close(): void {
    this.closed = true
  }

  emit(type: string, data: string, lastEventId = ''): void {
    const event = { type, data, lastEventId } as MessageEvent
    for (const cb of this.listeners[type] ?? []) cb(event)
  }

  fail(): void {
    this.onerror?.(new Event('error'))
  }
}

function makeWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { wrapper, invalidateSpy }
}

function invalidateCallsFor(spy: ReturnType<typeof vi.fn>, key: unknown): number {
  return spy.mock.calls.filter(
    (call) => JSON.stringify((call[0] as { queryKey: unknown }).queryKey) === JSON.stringify(key),
  ).length
}

beforeEach(() => {
  MockEventSource.instances = []
  ;(globalThis as unknown as { EventSource: unknown }).EventSource =
    MockEventSource as unknown
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useProjectEventStream', () => {
  it('opens a credentialed stream to the project endpoint', () => {
    const { wrapper } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })

    expect(MockEventSource.instances).toHaveLength(1)
    const source = at(MockEventSource.instances, 0)
    expect(source.url).toContain('/api/v1/projects/demo/events/stream')
    expect(source.withCredentials).toBe(true)
  })

  it('does not open a stream without a slug', () => {
    const { wrapper } = makeWrapper()
    const { result } = renderHook(() => useProjectEventStream(undefined), { wrapper })
    expect(MockEventSource.instances).toHaveLength(0)
    expect(result.current).toBe('closed')
  })

  it('reports "live" on hello(redis) and "degraded" on hello(non-redis)', () => {
    const { wrapper } = makeWrapper()
    const { result } = renderHook(() => useProjectEventStream('demo'), { wrapper })
    const source = at(MockEventSource.instances, 0)

    act(() => source.emit('hello', JSON.stringify({ backend: 'redis' }), '0'))
    expect(result.current).toBe('live')

    act(() => source.emit('hello', JSON.stringify({ backend: 'degraded' }), '0'))
    expect(result.current).toBe('degraded')
  })

  it('invalidates mapped keys on a project event', () => {
    const { wrapper, invalidateSpy } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    const source = at(MockEventSource.instances, 0)

    act(() =>
      source.emit('scan_job.updated', JSON.stringify({ project_slug: 'demo' }), '1'),
    )

    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo'])).toBe(1)
    expect(invalidateCallsFor(invalidateSpy, ['activity', 'demo'])).toBe(1)
  })

  it('de-duplicates a replayed event by id (no duplicate invalidation)', () => {
    const { wrapper, invalidateSpy } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    const source = at(MockEventSource.instances, 0)

    act(() => source.emit('signals.updated', '{}', '5'))
    act(() => source.emit('signals.updated', '{}', '5')) // replayed, same id

    expect(invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])).toBe(1)
  })

  it('reconnects with backoff carrying the Last-Event-ID cursor', () => {
    vi.useFakeTimers()
    const { wrapper } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    const first = at(MockEventSource.instances, 0)

    // Advance the cursor, then drop the connection.
    act(() => first.emit('scan_job.updated', '{}', '9'))
    act(() => first.fail())
    expect(first.closed).toBe(true)
    expect(MockEventSource.instances).toHaveLength(1)

    // Backoff (1 s, up to +30% jitter) elapses → a new stream opens, resuming
    // from the last id.
    act(() => {
      vi.advanceTimersByTime(1300)
    })
    expect(MockEventSource.instances).toHaveLength(2)
    expect(at(MockEventSource.instances, 1).url).toContain('last_event_id=9')
  })

  it('starts a fresh cursor and event sequence after switching projects', () => {
    const { wrapper, invalidateSpy } = makeWrapper()
    const { rerender } = renderHook(({ slug }) => useProjectEventStream(slug), {
      wrapper,
      initialProps: { slug: 'demo-a' },
    })
    const first = at(MockEventSource.instances, 0)

    act(() => first.emit('scan_job.updated', '{}', '9'))
    rerender({ slug: 'demo-b' })

    expect(first.closed).toBe(true)
    const second = at(MockEventSource.instances, 1)
    expect(second.url).toContain('/api/v1/projects/demo-b/events/stream')
    expect(second.url).not.toContain('last_event_id=9')

    act(() => second.emit('scan_job.updated', '{}', '1'))
    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo-b'])).toBe(1)
  })

  it('closes the stream on unmount', () => {
    const { wrapper } = makeWrapper()
    const { unmount } = renderHook(() => useProjectEventStream('demo'), { wrapper })
    const source = at(MockEventSource.instances, 0)
    unmount()
    expect(source.closed).toBe(true)
  })

  it('resyncs once on the hello that follows a reconnect', () => {
    vi.useFakeTimers()
    const { wrapper, invalidateSpy } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    const first = at(MockEventSource.instances, 0)
    act(() => first.emit('hello', JSON.stringify({ backend: 'redis' }), '0'))
    // The first hello of a stream is not a resync.
    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo'])).toBe(0)

    act(() => first.fail())
    act(() => {
      vi.advanceTimersByTime(1300)
    })
    const second = at(MockEventSource.instances, 1)
    act(() => second.emit('hello', JSON.stringify({ backend: 'redis' }), '0'))

    // More was missed than the replay ring may hold: every cache the stream
    // feeds is refreshed.
    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo'])).toBeGreaterThan(0)
    expect(invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])).toBeGreaterThan(0)
    expect(invalidateCallsFor(invalidateSpy, ['alertInbox', 'demo'])).toBeGreaterThan(0)
  })

  /** Open a stream, greet it with `firstSeq`, drop it, and open the reconnect. */
  function reconnectAfterHello(firstSeq: number) {
    vi.useFakeTimers()
    const { wrapper, invalidateSpy } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    const first = at(MockEventSource.instances, 0)
    act(() =>
      first.emit('hello', JSON.stringify({ backend: 'redis', seq: firstSeq, epoch: 'e1', buffer_size: 50 }), '0'),
    )
    act(() => first.fail())
    act(() => {
      vi.advanceTimersByTime(1300)
    })
    return { invalidateSpy, second: at(MockEventSource.instances, 1) }
  }

  it("starts the cursor at the first hello's sequence number", () => {
    const { second } = reconnectAfterHello(10)
    // No event arrived on the first stream, yet the reconnect still asks for
    // everything past what the page loaded.
    expect(second.url).toContain('last_event_id=10')
  })

  it('skips the resync when the replay ring covers the gap, and applies the replay', () => {
    const { invalidateSpy, second } = reconnectAfterHello(10)

    act(() =>
      second.emit('hello', JSON.stringify({ backend: 'redis', seq: 12, epoch: 'e1', buffer_size: 50 }), '0'),
    )
    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo'])).toBe(0)
    expect(invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])).toBe(0)

    // The two missed events arrive as replay right after the hello.
    act(() => second.emit('scan_job.updated', '{}', '11'))
    act(() => second.emit('signals.updated', '{}', '12'))
    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo'])).toBe(1)
    expect(invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])).toBe(1)
  })

  it('resyncs when a promised replay arrives with a hole', () => {
    const { invalidateSpy, second } = reconnectAfterHello(10)

    act(() =>
      second.emit('hello', JSON.stringify({ backend: 'redis', seq: 13, epoch: 'e1', buffer_size: 50 }), '0'),
    )
    expect(invalidateCallsFor(invalidateSpy, ['alertInbox', 'demo'])).toBe(0)
    act(() => second.emit('signals.updated', '{}', '11'))
    // 12 never arrives: the ring never kept it (its buffer write failed).
    act(() => second.emit('signals.updated', '{}', '13'))

    expect(invalidateCallsFor(invalidateSpy, ['alertInbox', 'demo'])).toBeGreaterThan(0)
  })

  it('resyncs when a live event overtakes the promised replay', () => {
    const { invalidateSpy, second } = reconnectAfterHello(10)

    act(() =>
      second.emit('hello', JSON.stringify({ backend: 'redis', seq: 12, epoch: 'e1', buffer_size: 50 }), '0'),
    )
    act(() => second.emit('signals.updated', '{}', '14'))

    expect(invalidateCallsFor(invalidateSpy, ['alertInbox', 'demo'])).toBeGreaterThan(0)
  })

  it('resyncs when the sequence has a new epoch, even past the cursor', () => {
    const { invalidateSpy, second } = reconnectAfterHello(10)

    // Redis restarted without persistence and the new sequence reached 20.
    act(() =>
      second.emit('hello', JSON.stringify({ backend: 'redis', seq: 20, epoch: 'e2', buffer_size: 50 }), '0'),
    )

    expect(invalidateCallsFor(invalidateSpy, ['alertInbox', 'demo'])).toBeGreaterThan(0)
  })

  it('resyncs when more was missed than the replay ring holds', () => {
    const { invalidateSpy, second } = reconnectAfterHello(10)

    act(() =>
      second.emit('hello', JSON.stringify({ backend: 'redis', seq: 61, epoch: 'e1', buffer_size: 50 }), '0'),
    )

    expect(invalidateCallsFor(invalidateSpy, ['scans', 'demo'])).toBeGreaterThan(0)
    expect(invalidateCallsFor(invalidateSpy, ['alertInbox', 'demo'])).toBeGreaterThan(0)
  })

  it('resyncs, and counts restarted ids again, when the sequence fell below the cursor', () => {
    const { invalidateSpy, second } = reconnectAfterHello(40)

    // Redis restarted without persistence: the project is back at 3.
    act(() =>
      second.emit('hello', JSON.stringify({ backend: 'redis', seq: 3, epoch: 'e1', buffer_size: 50 }), '0'),
    )
    const afterResync = invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])
    expect(afterResync).toBeGreaterThan(0)

    act(() => second.emit('signals.updated', '{}', '4'))
    expect(invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])).toBe(afterResync + 1)
  })

  it('treats an id far below the last one as a sequence reset, not a duplicate', () => {
    const { wrapper, invalidateSpy } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    const source = at(MockEventSource.instances, 0)

    act(() => source.emit('signals.updated', '{}', '5000'))
    // Redis restarted without persistence: the sequence starts again at 1.
    act(() => source.emit('signals.updated', '{}', '1'))

    expect(invalidateCallsFor(invalidateSpy, ['activeSignals', 'demo'])).toBe(2)
  })

  it('reconnects at once when the browser comes back online', () => {
    vi.useFakeTimers()
    const { wrapper } = makeWrapper()
    renderHook(() => useProjectEventStream('demo'), { wrapper })
    act(() => at(MockEventSource.instances, 0).fail())
    expect(MockEventSource.instances).toHaveLength(1)

    act(() => {
      window.dispatchEvent(new Event('online'))
    })

    expect(MockEventSource.instances).toHaveLength(2)
  })

  it('says the replay covers a gap only when it can tell', () => {
    const redis = (seq: number | null) => ({ backend: 'redis', seq, epoch: 'e1', bufferSize: 50 })
    expect(replayCoversGap(redis(10), '10', 'e1')).toBe(true)
    expect(replayCoversGap(redis(60), '10', 'e1')).toBe(true)
    expect(replayCoversGap(redis(61), '10', 'e1')).toBe(false)
    expect(replayCoversGap(redis(3), '10', 'e1')).toBe(false)
    expect(replayCoversGap(redis(null), '10', 'e1')).toBe(false)
    expect(replayCoversGap(redis(10), null, 'e1')).toBe(false)
    expect(replayCoversGap({ backend: 'degraded', seq: 10, epoch: 'e1', bufferSize: 50 }, '10', 'e1')).toBe(false)
    // Another epoch: Redis lost the sequence, whatever the numbers say.
    expect(replayCoversGap(redis(12), '10', 'e0')).toBe(false)
    expect(replayCoversGap(redis(12), '10', null)).toBe(false)
  })

  it('jitters the backoff within ±30% and caps it', () => {
    expect(reconnectDelay(0, () => 0)).toBe(700)
    expect(reconnectDelay(0, () => 1)).toBe(1300)
    expect(reconnectDelay(10, () => 0.5)).toBe(30_000)
  })
})
