import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DB_TYPE_OPTIONS, dbTypeLabel, isPreviewDbType } from '@/types'
import { DB_TYPE_PICKER_OPTIONS, dbTypePreviewText } from './db-type-picker'
import { DbTypePreviewChip, DbTypePreviewNote } from './db-type-preview'

describe('warehouse connectors in preview', () => {
  it('are the four whose live conformance suite has not passed yet', () => {
    expect(DB_TYPE_OPTIONS.filter(option => option.preview).map(option => option.value)).toEqual([
      'snowflake',
      'redshift',
      'trino',
      'athena',
    ])
    expect(isPreviewDbType('snowflake')).toBe(true)
    expect(isPreviewDbType('clickhouse')).toBe(false)
    expect(isPreviewDbType('synthetic')).toBe(false)
  })

  it('say so in the picker option, while the label stays the bare name', () => {
    expect(DB_TYPE_PICKER_OPTIONS).toContainEqual({ value: 'redshift', label: 'Amazon Redshift (preview)' })
    expect(DB_TYPE_PICKER_OPTIONS).toContainEqual({ value: 'postgres', label: 'PostgreSQL' })
    expect(dbTypeLabel('redshift')).toBe('Amazon Redshift')
  })

  it('explain what preview means under the picker, and nothing for a verified one', () => {
    const { rerender, container } = render(<DbTypePreviewNote dbType="trino" />)
    expect(
      screen.getByText(
        'Trino / Starburst support is in preview. It has not yet been verified against a live warehouse.',
      ),
    ).toBeInTheDocument()

    rerender(<DbTypePreviewNote dbType="bigquery" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('mark a source card, with the meaning in its tooltip', () => {
    const { rerender, container } = render(<DbTypePreviewChip dbType="snowflake" />)
    expect(screen.getByText('Preview')).toHaveAttribute('title', dbTypePreviewText('snowflake'))

    rerender(<DbTypePreviewChip dbType="databricks" />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('dbTypeLabel', () => {
  it('names the warehouse, and falls back to the wire id for one the picker lacks', () => {
    expect(dbTypeLabel('clickhouse')).toBe('ClickHouse')
    expect(dbTypeLabel('synthetic')).toBe('synthetic')
  })
})
