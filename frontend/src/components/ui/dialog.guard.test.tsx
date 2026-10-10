import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useDialogDirty, useDialogLeave } from './dialog-guard'
import { Dialog, DialogClose, DialogContent, DialogTitle } from './dialog'

/** A dialog whose owner holds the form state and passes `dirty`. */
function OwnedForm({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState('')
  return (
    <Dialog open dirty={name !== ''} onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent>
        <DialogTitle>New property</DialogTitle>
        <label>
          Name
          <input value={name} onChange={e => setName(e.target.value)} />
        </label>
        <DialogClose asChild>
          <button type="button">Cancel</button>
        </DialogClose>
      </DialogContent>
    </Dialog>
  )
}

/** A body that owns its state and reports it with `useDialogDirty`. */
function Body({ onLeave }: { onLeave: () => void }) {
  const [note, setNote] = useState('')
  useDialogDirty(note !== '')
  const leave = useDialogLeave()
  return (
    <DialogContent>
      <DialogTitle>Verdict</DialogTitle>
      <label>
        Note
        <input value={note} onChange={e => setNote(e.target.value)} />
      </label>
      <button type="button" onClick={() => leave(onLeave)}>
        Open page
      </button>
    </DialogContent>
  )
}

function BodyForm({ onLeave = () => {} }: { onLeave?: () => void }) {
  const [open, setOpen] = useState(true)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Reopen
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        {open && <Body onLeave={onLeave} />}
      </Dialog>
    </>
  )
}

const escape = () => fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

describe('Dialog unsaved-input guard', () => {
  it('closes a pristine form at once', () => {
    const onClose = vi.fn()
    render(<OwnedForm onClose={onClose} />)
    escape()
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('asks before Escape drops typed input, and keeps it on "Keep editing"', async () => {
    const onClose = vi.fn()
    render(<OwnedForm onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'spot_id' } })
    escape()

    const confirm = await screen.findByRole('alertdialog')
    expect(confirm).toHaveTextContent('Leave without saving?')
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Name')).toHaveValue('spot_id')
  })

  it('routes a <DialogClose> Cancel through the same question', async () => {
    const onClose = vi.fn()
    render(<OwnedForm onClose={onClose} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'spot_id' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('guards a body that reports its own state, and forgets it once the body is gone', async () => {
    render(<BodyForm />)
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'iOS stopped sending it' } })
    escape()
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    // A fresh body starts clean: the closed one's report went with it.
    fireEvent.click(screen.getByRole('button', { name: 'Reopen' }))
    expect(screen.getByLabelText('Note')).toHaveValue('')
    escape()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('asks before a way out that is not a close, such as a link to another page', async () => {
    const onLeave = vi.fn()
    render(<BodyForm onLeave={onLeave} />)

    fireEvent.click(screen.getByRole('button', { name: 'Open page' }))
    expect(onLeave).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Open page' }))
    expect(onLeave).toHaveBeenCalledTimes(1)
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onLeave).toHaveBeenCalledTimes(2))
  })

  it('never asks when the owner closes it by setting `open`, as after a save', async () => {
    function Saved() {
      const [open, setOpen] = useState(true)
      return (
        <Dialog open={open} dirty onOpenChange={setOpen}>
          <DialogContent>
            <DialogTitle>Edit</DialogTitle>
            <button type="button" onClick={() => setOpen(false)}>
              Save
            </button>
          </DialogContent>
        </Dialog>
      )
    }
    render(<Saved />)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })
})
