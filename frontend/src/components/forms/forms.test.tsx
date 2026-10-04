import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Label } from '@/components/ui/label'
import { FieldError } from './FieldError'
import { examplePlaceholder, sqlPlaceholder } from './placeholders'
import { SaveBar } from './SaveBar'
import {
  attentionSummary,
  focusFirstInvalid,
  invalidAria,
  missingSummary,
  REQUIRED_MESSAGE,
} from './validation'

describe('FieldError + invalidAria', () => {
  function Row({ error }: { error?: string }) {
    return (
      <div>
        <label htmlFor="name">Name</label>
        <input id="name" {...invalidAria('name', error)} />
        <FieldError inputId="name" message={error} />
      </div>
    )
  }

  it('renders nothing and marks nothing while the field is valid', () => {
    const { container } = render(<Row />)
    expect(container.querySelector('[data-slot="field-error"]')).toBeNull()
    expect(screen.getByLabelText('Name')).not.toHaveAttribute('aria-invalid')
  })

  it('links the message to an invalid control', () => {
    render(<Row error={REQUIRED_MESSAGE} />)
    const input = screen.getByLabelText('Name')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Required')
  })

  it('announces only when asked, and honours an explicit id', () => {
    const { rerender } = render(<FieldError id="custom" message="Bad" />)
    expect(screen.getByText('Bad')).toHaveAttribute('id', 'custom')
    expect(screen.queryByRole('alert')).toBeNull()
    rerender(<FieldError id="custom" message="Bad" announce />)
    expect(screen.getByRole('alert')).toHaveTextContent('Bad')
  })
})

describe('validation summaries', () => {
  it('names what is missing, or nothing', () => {
    expect(missingSummary([])).toBeNull()
    expect(missingSummary(['Name', 'Screen name'])).toBe('Fill in: Name, Screen name')
  })

  it('counts fields that need attention', () => {
    expect(attentionSummary(0)).toBeNull()
    expect(attentionSummary(1)).toBe('1 field needs attention')
    expect(attentionSummary(3)).toBe('3 fields need attention')
  })

  it('focuses the first invalid control in document order', () => {
    render(
      <form aria-label="F">
        <input aria-label="A" />
        <input aria-label="B" id="b" aria-invalid="true" />
        <input aria-label="C" aria-invalid="true" />
      </form>,
    )
    expect(focusFirstInvalid(screen.getByRole('form', { name: 'F' }))).toBe(true)
    expect(screen.getByLabelText('B')).toHaveFocus()
  })

  it('reports when nothing is invalid', () => {
    render(<input aria-label="A" />)
    expect(focusFirstInvalid()).toBe(false)
  })
})

describe('placeholders', () => {
  it('marks a single-line example', () => {
    expect(examplePlaceholder('created_at')).toBe('e.g. created_at')
    expect(examplePlaceholder('%', 'ms', '$')).toBe('e.g. %, ms, $')
  })

  it('comments out every line of a SQL example', () => {
    expect(sqlPlaceholder('Return a time column, e.g.', 'SELECT 1\n\nFROM t')).toBe(
      '-- Return a time column, e.g.\n-- SELECT 1\n--\n-- FROM t',
    )
    expect(sqlPlaceholder('Write a query')).toBe('-- Write a query')
  })
})

describe('SaveBar', () => {
  it('holds the actions and a live status line', () => {
    render(
      <SaveBar status="Fill in: Name" statusTone="danger">
        <button type="button">Cancel</button>
        <button type="submit">Save</button>
      </SaveBar>,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Fill in: Name')
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps the live region mounted when there is no status', () => {
    render(
      <SaveBar>
        <button type="submit">Save</button>
      </SaveBar>,
    )
    expect(screen.getByRole('status')).toBeEmptyDOMElement()
  })

  it('turns the status into a jump button and announces a save error', () => {
    const jump = vi.fn()
    render(
      <SaveBar status="3 fields need attention" onStatusClick={jump} error="Could not save" placement="top">
        <button type="submit">Save</button>
      </SaveBar>,
    )
    fireEvent.click(screen.getByRole('button', { name: '3 fields need attention' }))
    expect(jump).toHaveBeenCalledOnce()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not save')
  })
})

describe('Label optional suffix', () => {
  it('appends a muted "(optional)" only when asked', () => {
    const { rerender } = render(
      <>
        <Label htmlFor="f">From address</Label>
        <input id="f" />
      </>,
    )
    expect(screen.getByLabelText('From address')).toBeInTheDocument()
    rerender(
      <>
        <Label htmlFor="f" optional>
          From address
        </Label>
        <input id="f" />
      </>,
    )
    expect(screen.getByLabelText('From address (optional)')).toBeInTheDocument()
  })
})
