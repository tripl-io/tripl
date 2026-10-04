import { afterEach, describe, expect, it, vi } from 'vitest'
import { scansApi } from './scans'

// The real client is exercised; only global fetch is stubbed, so the messages
// asserted here are the ones ScanPreviewPanel actually renders.

const DRAFT = { data_source_id: 'ds-1', base_query: 'SELECT 1' }

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: '',
    headers: new Headers({ 'Content-Type': 'application/json' }),
    json: async () => body,
  } as unknown as Response
}

function dryRunJob(overrides: Record<string, unknown>) {
  return {
    id: 'dry-run-1',
    status: 'failed',
    result_summary: null,
    error_message: null,
    ...overrides,
  }
}

async function dryRunMessage(): Promise<string> {
  try {
    await scansApi.dryRun('demo', DRAFT)
  } catch (error) {
    return error instanceof Error ? error.message : String(error)
  }
  throw new Error('dryRun resolved, so there is no message to assert on')
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('scansApi.dryRun — what the user is told when the check does not answer', () => {
  it('never names the pipeline\'s own word for this at the user', async () => {
    // "Dry run" appears nowhere on the preview panel: the button says Check, the
    // wait says "Working out what this scan would create…", the answer says
    // "Would create N events". These two strings were the only place the
    // mechanism's name reached a screen.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(dryRunJob({})))

    const message = await dryRunMessage()
    expect(message).not.toMatch(/dry.?run/i)
    // ...and it does not restate the heading it renders under, which already
    // says "Could not work out what this scan would create".
    expect(message).not.toContain('work out what this scan would create')
    expect(message).toBe('The check stopped without saying why.')
  })

  it('prefers the reason the run itself gave, when it gave one', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse(dryRunJob({ error_message: 'Base query failed: table not found' })),
    )

    expect(await dryRunMessage()).toBe('Base query failed: table not found')
  })

  it('says the wait ran out in the panel\'s own words', async () => {
    // The deadline is checked before the first sleep, so a clock that has
    // already passed it exercises the timeout without waiting five minutes.
    const start = Date.now()
    vi.spyOn(Date, 'now').mockReturnValueOnce(start).mockReturnValue(start + 3_600_000)
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(dryRunJob({ status: 'running' })))

    const message = await dryRunMessage()
    expect(message).not.toMatch(/dry.?run/i)
    expect(message).toBe('Timed out working out what this scan would create.')
  })
})

function errorResponse(status: number): Response {
  return {
    ok: false,
    status,
    statusText: 'Bad Gateway',
    headers: new Headers({ 'Content-Type': 'application/json' }),
    json: async () => ({}),
  } as unknown as Response
}

describe('scansApi polling — a blip is not an answer', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('rides out a transient poll failure and returns the job result', async () => {
    vi.useFakeTimers()
    const answer = { events: [] }
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(dryRunJob({ status: 'running' })))
      .mockResolvedValueOnce(errorResponse(502))
      .mockResolvedValueOnce(jsonResponse(dryRunJob({ status: 'completed', result_summary: answer })))

    const pending = scansApi.dryRun('demo', DRAFT)
    await vi.advanceTimersByTimeAsync(60_000)

    await expect(pending).resolves.toEqual(answer)
  })

  it('gives up once the poll keeps failing', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(dryRunJob({ status: 'running' })))
      .mockResolvedValue(errorResponse(502))

    const pending = scansApi.dryRun('demo', DRAFT)
    const settled = expect(pending).rejects.toMatchObject({ status: 502 })
    await vi.advanceTimersByTimeAsync(60_000)
    await settled
  })

  it('does not retry an answer, such as the job being gone', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(dryRunJob({ status: 'running' })))
      .mockResolvedValue(errorResponse(404))
    vi.useFakeTimers()

    const pending = scansApi.dryRun('demo', DRAFT)
    const settled = expect(pending).rejects.toMatchObject({ status: 404 })
    await vi.advanceTimersByTimeAsync(60_000)
    await settled
    expect(fetchSpy).toHaveBeenCalledTimes(2)
  })

  it('stops waiting as soon as the caller aborts', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(dryRunJob({ status: 'running' })))
    const controller = new AbortController()

    const pending = scansApi.dryRun('demo', DRAFT, controller.signal)
    const settled = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(0)
    controller.abort()
    await settled
  })
})
