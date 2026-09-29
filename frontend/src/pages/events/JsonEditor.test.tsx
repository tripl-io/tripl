import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Variable } from '@/types'
import { JsonEditor } from './JsonEditor'

const VARIABLES: Variable[] = [
  {
    id: 'var-1',
    project_id: 'project-1',
    name: 'variant',
    source_name: null,
    variable_type: 'string',
    description: 'Experiment variant',
    allowed_values: ['control', 'treatment'],
    bindings: ['payload.variant'],
  },
]

describe('JsonEditor template authoring', () => {
  it('suggests canonical properties inside quoted JSON templates and accepts the value as valid JSON', () => {
    const onChange = vi.fn()
    render(<JsonEditor value="" onChange={onChange} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: '{"variant":"${' } })

    expect(screen.getByRole('option', { name: /\$\{variant\}/ })).toBeInTheDocument()
    expect(screen.getByText('payload.variant')).toBeInTheDocument()
    expect(screen.getByText('control · treatment')).toBeInTheDocument()

    fireEvent.change(editor, { target: { value: '{"variant":"${variant}"}' } })
    expect(editor).toHaveAttribute('aria-invalid', 'false')
  })

  it('rejects malformed JSON even when it contains a valid property token', () => {
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: '{"variant":"${variant}",}' } })

    expect(editor).toHaveAttribute('aria-invalid', 'true')
  })

  it('accepts the raw JSON-path tokens a scan writes', () => {
    // The backend's grammar is `^[^"\\}\x00-\x1f]+$` (tripl-0zpq.125): a scan
    // keeps the raw path whenever `derive_display_name` cannot sanitise it, so
    // these are tokens already sitting in stored field values. This editor
    // blocks the save through EventForm's `invalidJsonFieldLabels`, so while it
    // enforced the old identifier grammar such an event could not be re-saved
    // at all — not even an edit that only touched the description.
    //
    // RED on a revert: put `[A-Za-z_][A-Za-z0-9_.-]*` back into
    // `jsonTemplate.JSON_TEMPLATE_TOKEN_NAME_PATTERN` and the space, the comma
    // and the Cyrillic all fail it, so `templateJsonError` returns a message and
    // aria-invalid is 'true' on both values below.
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: '{"city":"${property.Albany, OR}"}' } })
    expect(editor).toHaveAttribute('aria-invalid', 'false')

    fireEvent.change(editor, { target: { value: '{"city":"${property.Москва}"}' } })
    expect(editor).toHaveAttribute('aria-invalid', 'false')
  })

  it('rejects a property token with JSON-breaking characters', () => {
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: '{"variant": ${bad"token}}' } })

    expect(editor).toHaveAttribute('aria-invalid', 'true')
  })

  it('rejects property templates used as object keys', () => {
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: '{"${variant}": "control"}' } })

    expect(editor).toHaveAttribute('aria-invalid', 'true')
  })

  it('formats templates without replacing a matching literal sentinel value', () => {
    const onChange = vi.fn()
    render(
      <JsonEditor
        value={'{"literal":"\\u005f_TRIPL_VAR_1__","a":"${variant}","b":"${variant}"}'}
        onChange={onChange}
        variables={VARIABLES}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Format' }))

    expect(onChange).toHaveBeenCalledWith(
      '{\n  "literal": "__TRIPL_VAR_1__",\n  "a": "${variant}",\n  "b": "${variant}"\n}',
    )
  })
  it('re-indents a templated value on mount, because the server stores JSON on one line', () => {
    render(
      <JsonEditor
        value={'{"from_profile": "${variant}", "mode": "dark"}'}
        onChange={vi.fn()}
        variables={VARIABLES}
      />,
    )

    expect(screen.getByRole('combobox')).toHaveValue(
      '{\n  "from_profile": "${variant}",\n  "mode": "dark"\n}',
    )
  })

  it('keeps a value it cannot parse rather than blanking the field', () => {
    render(<JsonEditor value="not json at all" onChange={vi.fn()} variables={VARIABLES} />)

    expect(screen.getByRole('combobox')).toHaveValue('not json at all')
    // ...and says so on mount. Validity used to start null and only be written
    // by a keystroke, so an untouched stored value read as valid (tripl-h2sx.10).
    expect(screen.getByRole('combobox')).toHaveAttribute('aria-invalid', 'true')
  })

  it('reports nothing on an empty field', () => {
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    expect(screen.getByRole('combobox')).toHaveAttribute('aria-invalid', 'false')
  })

  it('reports why Format refused instead of silently doing nothing', () => {
    const onChange = vi.fn()
    render(<JsonEditor value="" onChange={onChange} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: '{"variant": ' } })
    onChange.mockClear()

    fireEvent.click(screen.getByRole('button', { name: 'Format' }))

    expect(onChange).not.toHaveBeenCalled()
    expect(editor).toHaveAttribute('aria-invalid', 'true')
    const described = editor.getAttribute('aria-describedby')
    expect(described).toBeTruthy()
    expect(document.getElementById(described!)?.textContent).toBeTruthy()
  })

  it('keeps Format out of the text: it is a sibling control, not an overlay', () => {
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    const formatButton = screen.getByRole('button', { name: 'Format' })
    expect(formatButton).not.toHaveClass('absolute')
    expect(formatButton.parentElement).not.toContain(screen.getByRole('combobox'))
  })
  it('repairs loose input, says what it changed and can undo it', () => {
    const onChange = vi.fn()
    render(<JsonEditor value="" onChange={onChange} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    const loose = 'from_profile: property.forecast_profile, mode: property.mode'
    fireEvent.change(editor, { target: { value: loose } })
    expect(editor).toHaveAttribute('aria-invalid', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Format' }))

    expect(editor).toHaveValue(
      '{\n  "from_profile": "${property.forecast_profile}",\n  "mode": "${property.mode}"\n}',
    )
    expect(editor).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByText(/read 2 values as properties/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(editor).toHaveValue(loose)
    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
  })

  it('does not offer to repair a malformed property token', () => {
    render(<JsonEditor value="" onChange={vi.fn()} variables={VARIABLES} />)

    const editor = screen.getByRole('combobox')
    fireEvent.change(editor, { target: { value: 'a: ${bad"token}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Format' }))

    expect(screen.queryByRole('button', { name: 'Undo' })).not.toBeInTheDocument()
    expect(screen.getByText(/cannot contain a quote/)).toBeInTheDocument()
  })
})

describe('JsonEditor outside changes (EVT-22)', () => {
  it('shows a value the parent resets, as "Hand back to scans" does', () => {
    const { rerender } = render(<JsonEditor value='{"source":"cta"}' onChange={vi.fn()} />)
    const editor = screen.getByRole('combobox')
    expect(editor).toHaveValue('{\n  "source": "cta"\n}')

    rerender(<JsonEditor value="" onChange={vi.fn()} />)
    expect(editor).toHaveValue('')

    rerender(<JsonEditor value='{"source":"banner"}' onChange={vi.fn()} />)
    expect(editor).toHaveValue('{\n  "source": "banner"\n}')
  })

  it('keeps what is being typed when the parent echoes it back', () => {
    const onChange = vi.fn()
    const { rerender } = render(<JsonEditor value="" onChange={onChange} />)
    const editor = screen.getByRole('combobox')

    fireEvent.change(editor, { target: { value: '{"a": 1' } })
    rerender(<JsonEditor value='{"a": 1' onChange={onChange} />)
    // Not re-indented or replaced mid-edit: the echo is the editor's own text.
    expect(editor).toHaveValue('{"a": 1')

    fireEvent.change(editor, { target: { value: '   ' } })
    rerender(<JsonEditor value="" onChange={onChange} />)
    expect(editor).toHaveValue('   ')
  })
})
