import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'
import ConfirmDialog from '@/components/ConfirmDialog'
import { useConfirm, type ConfirmOptions } from './useConfirm'

type Confirm = (opts: ConfirmOptions) => Promise<boolean>

/** Mounts the hook and hands its `confirm` to the test on every render. */
function Harness({ onConfirm }: { onConfirm: (confirm: Confirm) => void }) {
  const { confirm, dialog } = useConfirm()
  useEffect(() => {
    onConfirm(confirm)
  })
  return <>{dialog}</>
}

function mount() {
  const seen: Confirm[] = []
  const utils = render(<Harness onConfirm={(c) => seen.push(c)} />)
  return { ...utils, seen, confirm: () => seen[seen.length - 1]! }
}

describe('useConfirm', () => {
  it('resolves true on Confirm and false on Cancel', async () => {
    const { confirm } = mount()

    let answer!: Promise<boolean>
    act(() => {
      answer = confirm()({ title: 'Delete it?', message: 'Gone for good.', confirmLabel: 'Delete' })
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await expect(answer).resolves.toBe(true)

    act(() => {
      answer = confirm()({ title: 'Delete it?', message: 'Gone for good.', confirmLabel: 'Delete' })
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await expect(answer).resolves.toBe(false)
  })

  it('answers a replaced request "no" instead of leaving it pending', async () => {
    const { confirm } = mount()

    let first!: Promise<boolean>
    let second!: Promise<boolean>
    act(() => {
      first = confirm()({ title: 'First?', message: 'one' })
    })
    act(() => {
      second = confirm()({ title: 'Second?', message: 'two' })
    })

    await expect(first).resolves.toBe(false)
    expect(await screen.findByRole('alertdialog', { name: 'Second?' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await expect(second).resolves.toBe(true)
  })

  it('keeps `confirm` the same function across renders', async () => {
    const { seen, confirm } = mount()
    act(() => {
      void confirm()({ title: 'Stable?', message: 'x' })
    })
    await screen.findByRole('alertdialog')
    expect(seen.length).toBeGreaterThan(1)
    expect(new Set(seen).size).toBe(1)
  })

  it('renders rich content as the description', async () => {
    const { confirm } = mount()
    act(() => {
      void confirm()({
        title: 'Mute?',
        message: (
          <ul>
            <li>Stops alerts</li>
          </ul>
        ),
      })
    })
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveAccessibleDescription('Stops alerts')
    expect(screen.getByRole('listitem')).toHaveTextContent('Stops alerts')
  })
})

describe('ConfirmDialog', () => {
  it('calls only onConfirm when Confirm is clicked', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(
      <ConfirmDialog open title="Sure?" message="Really." onConfirm={onConfirm} onCancel={onCancel} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('calls onCancel exactly once for Cancel, and once for Escape', () => {
    const onCancel = vi.fn()
    const { rerender } = render(
      <ConfirmDialog open title="Sure?" message="Really." onConfirm={vi.fn()} onCancel={onCancel} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)

    onCancel.mockClear()
    rerender(<ConfirmDialog open={false} title="Sure?" message="Really." onConfirm={vi.fn()} onCancel={onCancel} />)
    rerender(<ConfirmDialog open title="Sure?" message="Really." onConfirm={vi.fn()} onCancel={onCancel} />)
    fireEvent.keyDown(screen.getByRole('alertdialog'), { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
