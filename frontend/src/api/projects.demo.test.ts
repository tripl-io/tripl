import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { projectsApi } from './projects'

// The real client is exercised; only global fetch is stubbed. Demo creation
// answers 202 with a seeding shell and the worker seeds it, so createDemo
// re-reads the shell until it settles.

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function shell(generation_status: string, extra: Record<string, unknown> = {}) {
  return { id: 'p1', slug: 'demo-abc123', name: 'Demo', is_demo: true, generation_status, ...extra }
}

function paths(spy: { mock: { calls: unknown[][] } }): string[] {
  return spy.mock.calls.map((call) => new URL(String(call[0]), 'http://localhost').pathname)
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('projectsApi.createDemo', () => {
  it('polls the seeding shell until the worker marks it ready', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(shell('seeding'), 202))
      .mockResolvedValueOnce(jsonResponse(shell('seeding')))
      .mockResolvedValueOnce(jsonResponse(shell('ready')))

    const project = await projectsApi.createDemo(undefined, 1)

    expect(project.generation_status).toBe('ready')
    expect(paths(fetchSpy)).toEqual([
      '/api/v1/projects/demo',
      '/api/v1/projects/demo-abc123',
      '/api/v1/projects/demo-abc123',
    ])
  })

  it('returns at once when the create already answers ready', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse(shell('ready'), 202))

    await expect(projectsApi.createDemo(undefined, 1)).resolves.toMatchObject({ slug: 'demo-abc123' })
    expect(fetchSpy).toHaveBeenCalledTimes(1)
  })

  it('rejects with the seed failure the worker recorded', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(shell('seeding'), 202))
      .mockResolvedValueOnce(jsonResponse(shell('failed', { generation_error: 'Seeding failed.' })))

    const error = await projectsApi.createDemo(undefined, 1).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(500)
    expect((error as ApiError).message).toBe('Seeding failed.')
  })

  it('reads a shell that vanished mid-seed as a cancel', async () => {
    // A cancel from another tab deletes the shell; the hook tells that apart
    // from the demo limit by this 409's message.
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(shell('seeding'), 202))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Project not found' }, 404))

    const error = await projectsApi.createDemo(undefined, 1).catch((caught: unknown) => caught)

    expect((error as ApiError).status).toBe(409)
    expect((error as ApiError).message).toMatch(/provisioning was cancelled/i)
  })

  it('stops polling when the caller aborts', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(shell('seeding'), 202))
    const controller = new AbortController()

    const pending = projectsApi.createDemo(controller.signal, 60_000)
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1))
    controller.abort()

    const error = await pending.catch((caught: unknown) => caught)
    expect((error as ApiError).status).toBe(408)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
  })
})
