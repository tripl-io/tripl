import type { ReactNode } from 'react'
import { ExternalLink, Rocket, Tag, Trash2 } from 'lucide-react'
import { Chip } from '@/components/primitives/chip'
import { IconButton } from '@/components/ui/icon-button'
import {
  annotationMarkerColor,
  annotationSourceLabel,
  isAutomaticAnnotation,
  safeAnnotationUrl,
} from '@/lib/chartAnnotations'
import { formatTimestamp } from '@/lib/datetime'
import type { ChartAnnotation } from '@/types'

/**
 * One annotation in a list: its marker colour, time, label, who made it and
 * its link, with a delete for an editor. Shared by a chart's Annotations card
 * and the Annotations page, which differ only in how they show the scope.
 */
export function AnnotationItem({
  annotation,
  scope,
  onDelete,
  deleting = false,
  inset = false,
}: {
  annotation: ChartAnnotation
  /** Where the marker is drawn: a link to its chart, or a project-wide chip. */
  scope?: ReactNode
  /** Omitted when the reader cannot delete. */
  onDelete?: () => void
  deleting?: boolean
  /** Pad the row itself, for a list that sits flush in a panel. */
  inset?: boolean
}) {
  const automatic = isAutomaticAnnotation(annotation)
  const url = safeAnnotationUrl(annotation.url)
  return (
    <li className={`flex items-center justify-between gap-2 py-2${inset ? ' px-4' : ''}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span
          aria-hidden="true"
          className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
          style={{ backgroundColor: annotationMarkerColor(annotation) }}
        />
        {/* Local time, like every instant; the title names the zone, since the
            holidays listed beside it are UTC days. */}
        <time
          dateTime={annotation.bucket}
          title={formatTimestamp(annotation.bucket, { zone: true })}
          className="text-fg-tertiary"
        >
          {formatTimestamp(annotation.bucket)}
        </time>
        <span className={`min-w-0 break-words font-medium${automatic ? ' text-fg-secondary' : ''}`}>
          {annotation.label}
        </span>
        {/* Who made it: the metrics worker or a deploy script, not someone
            in a form (#256). */}
        {automatic && (
          <Chip
            variant="outline"
            size="xs"
            icon={annotation.source === 'release' ? <Tag aria-hidden="true" /> : <Rocket aria-hidden="true" />}
          >
            {annotationSourceLabel(annotation.source)}
          </Chip>
        )}
        {scope}
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Details for ${annotation.label} (opens in a new tab)`}
            className="inline-flex items-center gap-1 text-caption text-fg-tertiary hover:text-fg underline-offset-2 hover:underline"
          >
            Details
            <ExternalLink aria-hidden="true" className="size-3" />
          </a>
        )}
      </div>
      {onDelete && (
        <IconButton
          variant="ghost"
          className="h-7 w-7 shrink-0 text-fg-tertiary hover:text-destructive"
          onClick={onDelete}
          // Only the row being deleted waits, not every row.
          disabled={deleting}
          label={`Delete annotation ${annotation.label}`}
        >
          <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
        </IconButton>
      )}
    </li>
  )
}
