import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { STRING_FORMAT_LABELS, STRING_FORMATS, defaultSchemaFor, stringFormatLabel } from '@/lib/propertySchema'
import type { VariableType } from '@/types'
import { PropertySchemaEditor } from './PropertySchemaEditor'

function renderEditor(variableType: VariableType) {
  return render(
    <PropertySchemaEditor
      variableType={variableType}
      schema={defaultSchemaFor(variableType)}
      onChange={vi.fn()}
      problems={[]}
    />,
  )
}

function optionLabels(select: HTMLElement): string[] {
  return within(select).getAllByRole('option').map((option) => option.textContent ?? '')
}

describe('PropertySchemaEditor: one type picker, readable formats', () => {
  it("shows no second type picker when the property's Type already decides it", () => {
    renderEditor('string')
    expect(screen.queryByLabelText('Schema type of the property')).toBeNull()
    // A plain string offers every format but the date ones, by name.
    expect(optionLabels(screen.getByLabelText('Format of the property'))).toEqual([
      'Any text',
      'Time',
      'Duration (ISO 8601)',
      'Email address',
      'URL',
      'UUID',
      'Hostname',
      'IPv4 address',
      'IPv6 address',
    ])
  })

  it('keeps the picker where it is a real choice', () => {
    renderEditor('number')
    expect(optionLabels(screen.getByLabelText('Schema type of the property'))).toEqual(['Number', 'Integer'])
  })

  it("names a date type's pinned format, keeping the schema id as the value", () => {
    renderEditor('datetime')
    const format = screen.getByLabelText('Format of the property')
    expect(format).toBeDisabled()
    expect(format).toHaveValue('date-time')
    expect(optionLabels(format)).toEqual(['Date and time (ISO 8601)'])
  })

  it('shows an array of strings its items, with no pinned type pickers', () => {
    renderEditor('string_array')
    expect(screen.queryByLabelText('Schema type of the property')).toBeNull()
    expect(screen.queryByLabelText('Schema type of items of the property')).toBeNull()
    expect(screen.getByLabelText('Format of items of the property')).toBeInTheDocument()
  })

  it('says a boolean has nothing more to describe instead of an empty box', () => {
    renderEditor('boolean')
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.getByText('True or false: there is nothing more to describe.')).toBeInTheDocument()
  })
})

describe('stringFormatLabel', () => {
  it('labels every format the backend accepts, and reads an unknown one as itself', () => {
    for (const format of STRING_FORMATS) expect(STRING_FORMAT_LABELS[format]).toBeTruthy()
    expect(stringFormatLabel('uri')).toBe('URL')
    expect(stringFormatLabel('iri')).toBe('iri')
  })
})
