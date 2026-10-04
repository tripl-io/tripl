import { SegmentedControl } from '@/components/ui/segmented-control'
import { RANGE_OPTIONS } from '@/lib/metrics'

/**
 * The 7d / 30d / 90d range picker — one segmented group, the shape every chart
 * header uses. Drawn with the shared SegmentedControl: a
 * raised option on a sunken track instead of a solid accent fill, and 32px
 * tall (the old 24px/11px options were below any tap target), so it
 * lines up with a default SelectTrigger beside it. `size="sm"` (28px) sits
 * next to `size="sm"` buttons and `h-7` selects.
 */
export function RangeSegmentedControl({
  value,
  onChange,
  options = RANGE_OPTIONS,
  size = 'md',
  className,
}: {
  value: number
  onChange: (days: number) => void
  options?: ReadonlyArray<{ label: string; days: number }>
  size?: 'sm' | 'md'
  className?: string
}) {
  return (
    <SegmentedControl
      aria-label="Time range"
      value={value}
      onChange={onChange}
      options={options.map(option => ({ value: option.days, label: option.label }))}
      size={size}
      className={className}
    />
  )
}
