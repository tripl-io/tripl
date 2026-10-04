import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { factTablesApi } from '@/api/factTables'
import type { SelectOption } from '@/components/settings/kit'
import type { DataSource } from '@/types'
import type { DbType } from '@/types/dataSources'
import type { TableSchema } from '@/types/dataSourceSchema'
import type { FactTable, FactTableColumn } from '@/types/factTables'
import type { MetricDraft } from './metricDraft'
import { factTableKey, factTablesKey } from '@/lib/queryKeys'

/**
 * The slice of a loaded fact table one operand editor needs: its columns, the
 * identifier columns / named row filters that populate the dropdowns, and the
 * dialect + one-table schema its SQL filter editor highlights and completes
 * against. A filter runs over the fact table's own output
 * (`SELECT * FROM (<fact sql>) AS _filtered WHERE …`), so the fact table's
 * columns are the right completion set — not every table in the warehouse.
 */
export interface FactTableDetail {
  columns: FactTableColumn[]
  /** The column the table buckets by, never a breakdown. */
  timestampColumn?: string
  identifierColumns: string[]
  rowFilters: string[]
  dialect?: DbType
  tables?: TableSchema[]
}

const EMPTY_DETAIL: FactTableDetail = { columns: [], identifierColumns: [], rowFilters: [] }

function toDetail(table: FactTable | undefined, dataSources: readonly DataSource[]): FactTableDetail {
  if (!table) return EMPTY_DETAIL
  return {
    columns: table.columns,
    timestampColumn: table.timestamp_column,
    identifierColumns: table.identifier_columns,
    rowFilters: table.row_filters.map(filter => filter.name),
    dialect: dataSources.find(source => source.id === table.data_source_id)?.db_type,
    tables: [
      {
        name: table.name,
        columns: table.columns.map(column => ({ name: column.name, data_type: column.type })),
      },
    ],
  }
}

export interface OperandDetailState {
  detail: FactTableDetail
  loading: boolean
  error: unknown
}

export interface FactTableDetails {
  factTableOptions: SelectOption[]
  /** The list loaded and is empty: there is nothing to aggregate yet. */
  noFactTables: boolean
  numerator: OperandDetailState
  denominator: OperandDetailState
  /** Column types still loading: serialising now would type numbers as strings. */
  loading: boolean
  error: unknown
}

/** Fact-table list + the detail of each operand's table, for a fact draft. */
export function useFactTableDetails(
  slug: string,
  draft: MetricDraft,
  dataSources: readonly DataSource[],
  /** Load the list for any kind: the create form's kind step reads it. */
  { loadList = false }: { loadList?: boolean } = {},
): FactTableDetails {
  const enabled = draft.kind === 'fact'
  const isRatio = draft.factComposition === 'ratio'
  const numeratorId = draft.numeratorOp.factTableId
  const denominatorId = draft.denominatorOp.factTableId

  const listQuery = useQuery({
    queryKey: factTablesKey(slug),
    queryFn: () => factTablesApi.list(slug),
    enabled: enabled || loadList,
  })
  const numeratorQuery = useQuery({
    queryKey: factTableKey(slug, numeratorId),
    queryFn: () => factTablesApi.get(slug, numeratorId),
    enabled: enabled && !!numeratorId,
  })
  const denominatorQuery = useQuery({
    queryKey: factTableKey(slug, denominatorId),
    queryFn: () => factTablesApi.get(slug, denominatorId),
    enabled: enabled && isRatio && !!denominatorId,
  })

  const factTableOptions = useMemo<SelectOption[]>(
    () => [
      { value: '', label: 'Select fact table…' },
      ...(listQuery.data?.items ?? []).map(t => ({ value: t.id, label: t.display_name })),
    ],
    [listQuery.data],
  )
  const numeratorDetail = useMemo(
    () => toDetail(numeratorQuery.data, dataSources),
    [numeratorQuery.data, dataSources],
  )
  const denominatorDetail = useMemo(
    () => toDetail(denominatorQuery.data, dataSources),
    [denominatorQuery.data, dataSources],
  )

  const numeratorLoading = enabled && !!numeratorId && numeratorQuery.isPending
  const denominatorLoading = enabled && isRatio && !!denominatorId && denominatorQuery.isPending
  const numeratorError = enabled ? numeratorQuery.error : null
  const denominatorError = enabled && isRatio ? denominatorQuery.error : null

  return {
    factTableOptions,
    noFactTables: listQuery.isSuccess && listQuery.data.items.length === 0,
    numerator: { detail: numeratorDetail, loading: numeratorLoading, error: numeratorError },
    denominator: { detail: denominatorDetail, loading: denominatorLoading, error: denominatorError },
    loading: numeratorLoading || denominatorLoading,
    error: numeratorError ?? denominatorError ?? null,
  }
}
