import { Chip, type ChipSize } from '@/components/primitives/chip'
import { freshnessChipContent } from '@/lib/sourceFreshness'
import type { SourceFreshness } from '@/types'

/**
 * A scan source's freshness as one pill (F16, #269): `late` reads amber
 * ("Data late · 7h"), `overdue` red ("Scan overdue"). A fresh source says
 * nothing — quiet is the healthy default — and so does `unknown` (a manual
 * scan, or one that has not collected yet): neither is a problem to flag.
 *
 * `name` prefixes the accessible label when several chips share a surface
 * (the Overview's source health list), so a screen reader hears whose source
 * is late.
 */
export function FreshnessChip({
  freshness,
  size = 'xs',
  name,
  className,
}: {
  freshness: SourceFreshness | null | undefined
  size?: ChipSize
  name?: string
  className?: string
}) {
  const content = freshnessChipContent(freshness)
  if (!content) return null
  const description = name ? `${name}: ${content.description}` : content.description
  return (
    <Chip
      tone={content.tone}
      size={size}
      title={description}
      aria-label={`${content.label}. ${description}`}
      data-freshness={freshness?.status}
      className={className}
    >
      {content.label}
    </Chip>
  )
}
