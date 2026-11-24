import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { IncidentReproduction } from './IncidentReproduction'

it('offers a local comparison and drafts only allowlisted evidence', async () => {
  const append = vi.fn()
  render(<IncidentReproduction initialEvent="purchase" append={append} />)
  fireEvent.click(screen.getByText("Reproduce tracking regression"))
  for (const role of ['healthy', 'suspect']) {
    fireEvent.change(screen.getByLabelText(role + ' artifact page'), { target: { value: 'https://ci.example/artifacts/' + role } })
  }
  expect(screen.getByText(/tripl reproduce/)).toHaveTextContent('--suspect suspect.json')
  fireEvent.change(screen.getByLabelText('CLI report JSON'), { target: { value: JSON.stringify({
    version: 1, event: 'purchase', journey: 'checkout', fixture: 'order1', verdict: 'reproduced',
    healthy: { build: 'r1', count: 1 }, suspect: { build: 'r2', count: 0 }, candidate: null,
    command: 'untrusted shell', unrelated: 'private payload',
  }) } })
  fireEvent.click(screen.getByRole('button', { name: 'Add evidence to note draft' }))
  expect(append).toHaveBeenCalledOnce()
  const [note] = append.mock.calls[0]!
  expect(note).toContain('r2')
  expect(note).toContain('healthy.json: https://ci.example/artifacts/healthy')
  expect(note).not.toMatch(/untrusted|private/)
})

it('rejects mismatched reports and unsafe event names', () => {
  const append = vi.fn()
  render(<IncidentReproduction initialEvent="purchase" append={append} />)
  fireEvent.click(screen.getByText("Reproduce tracking regression"))
  fireEvent.change(screen.getByLabelText('CLI report JSON'), { target: { value: '{}' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add evidence to note draft' }))
  expect(screen.getByRole('alert')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Tracked event'), { target: { value: 'purchase\nwhoami' } })
  expect(append).not.toHaveBeenCalled()
})

it('records missing builds as inconclusive and rejects signed artifact URLs', () => {
  const append = vi.fn()
  render(<IncidentReproduction initialEvent="purchase" append={append} />)
  fireEvent.click(screen.getByText('Reproduce tracking regression'))
  fireEvent.change(screen.getByLabelText('CLI report JSON'), { target: { value: JSON.stringify({
    version: 1, event: 'purchase', journey: null, fixture: null, verdict: 'inconclusive',
    healthy: null, suspect: { build: 'r2', count: 0 }, candidate: null, reason: 'Missing capture.',
  }) } })
  const suspect = screen.getByLabelText('suspect artifact page')
  fireEvent.change(suspect, { target: { value: 'https://ci.example/r2?token=secret' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add evidence to note draft' }))
  expect(screen.getByRole('alert')).toHaveTextContent('without credentials')
  expect(append).not.toHaveBeenCalled()
  fireEvent.change(suspect, { target: { value: 'https://ci.example/r2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add evidence to note draft' }))
  expect(append).toHaveBeenCalledWith(expect.stringContaining('healthy: missing capture'))
})
