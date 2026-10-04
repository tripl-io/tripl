import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Link, MemoryRouter, RouterProvider, createMemoryRouter, useNavigate } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { useUnsavedChangesGuard, useUnsavedDialogGuard } from './useUnsavedChangesGuard'

function Form() {
  const [value, setValue] = useState('')
  const navigate = useNavigate()
  const guard = useUnsavedChangesGuard(value !== '')
  return (
    <div>
      {guard.dialog}
      <label>
        Name
        <input value={value} onChange={e => setValue(e.target.value)} />
      </label>
      <Link to="/elsewhere">Leave</Link>
      <Link to="/form?tab=2">Same page</Link>
      <button
        type="button"
        onClick={() => {
          guard.release()
          navigate('/elsewhere')
        }}
      >
        Save
      </button>
    </div>
  )
}

function renderForm() {
  const router = createMemoryRouter(
    [
      { path: '/form', element: <Form /> },
      { path: '/elsewhere', element: <p>Elsewhere</p> },
    ],
    { initialEntries: ['/form'] },
  )
  render(<RouterProvider router={router} />)
  return router
}

describe('useUnsavedChangesGuard', () => {
  it('lets a pristine form leave without asking', async () => {
    renderForm()
    fireEvent.click(screen.getByRole('link', { name: 'Leave' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })

  it('asks before leaving a dirty form, and stays on Keep editing', async () => {
    const router = renderForm()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('link', { name: 'Leave' }))

    const dialog = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(dialog).toBeInTheDocument()
    // The safe answer is the default: Enter keeps the draft.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Keep editing' })).toHaveFocus())
    expect(screen.getByRole('button', { name: 'Discard changes' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(router.state.location.pathname).toBe('/form')
    expect(screen.getByLabelText('Name')).toHaveValue('draft')
  })

  it('leaves once the user accepts the loss', async () => {
    renderForm()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('link', { name: 'Leave' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })

  it('guards browser Back too', async () => {
    const router = createMemoryRouter(
      [
        { path: '/form', element: <Form /> },
        { path: '/elsewhere', element: <p>Elsewhere</p> },
      ],
      { initialEntries: ['/elsewhere', '/form'], initialIndex: 1 },
    )
    render(<RouterProvider router={router} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    await act(async () => {
      await router.navigate(-1)
    })
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(router.state.location.pathname).toBe('/form')
  })

  it('does not block a same-path navigation', async () => {
    const router = renderForm()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('link', { name: 'Same page' }))
    await waitFor(() => expect(router.state.location.search).toBe('?tab=2'))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('lets a released form navigate after a save without asking', async () => {
    renderForm()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('arms the browser prompt only while dirty', () => {
    renderForm()
    const pristine = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(pristine)
    expect(pristine.defaultPrevented).toBe(false)

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    const dirty = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirty)
    expect(dirty.defaultPrevented).toBe(true)
  })

  it('renders outside a data router without throwing', () => {
    render(
      <MemoryRouter>
        <Form />
      </MemoryRouter>,
    )
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
  })
})

function DialogForm({ onClosed }: { onClosed: () => void }) {
  const [value, setValue] = useState('')
  const guard = useUnsavedDialogGuard(value !== '')
  return (
    <div>
      {guard.dialog}
      <label>
        Name
        <input value={value} onChange={e => setValue(e.target.value)} />
      </label>
      <button type="button" onClick={() => guard.requestClose(onClosed)}>
        Close
      </button>
    </div>
  )
}

describe('useUnsavedDialogGuard', () => {
  it('closes a pristine dialog at once', async () => {
    let closed = false
    render(<DialogForm onClosed={() => { closed = true }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(closed).toBe(true))
  })

  it('asks before closing a dirty dialog', async () => {
    let closed = false
    render(<DialogForm onClosed={() => { closed = true }} />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(closed).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(closed).toBe(true))
  })
})
