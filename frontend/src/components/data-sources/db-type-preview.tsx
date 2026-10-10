import { Chip } from '@/components/primitives/chip'
import { isPreviewDbType, type DbType } from '@/types'
import { dbTypePreviewText } from './db-type-picker'

/** Said under the type picker while a preview connector is picked; nothing otherwise. */
export function DbTypePreviewNote({ dbType }: { dbType: DbType }) {
  if (!isPreviewDbType(dbType)) return null
  return <p className="text-caption text-fg-tertiary">{dbTypePreviewText(dbType)}</p>
}

/** The source card's marker for a connection to a preview connector. */
export function DbTypePreviewChip({ dbType }: { dbType: DbType }) {
  if (!isPreviewDbType(dbType)) return null
  return (
    <Chip tone="info" size="xs" title={dbTypePreviewText(dbType)}>
      Preview
    </Chip>
  )
}
