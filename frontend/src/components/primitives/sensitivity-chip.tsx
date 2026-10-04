import { Chip } from '@/components/primitives/chip'
import { SENSITIVITY_OPTIONS, SENSITIVITY_STYLE, type Sensitivity } from '@/types'

// Drawn with the shared Chip rather than a third hand-rolled pill: the
// sensitivity scale keeps its own colours, the shape and sizing are Chip's.
export function SensitivityChip({ value }: { value: Sensitivity }) {
  if (!value || value === 'none') {
    return <span className="text-fg-faint text-micro">—</span>
  }
  const style = SENSITIVITY_STYLE[value]
  const label = SENSITIVITY_OPTIONS.find((o) => o.value === value)?.label ?? value.toUpperCase()
  return (
    <Chip size="xs" className="font-semibold" style={{ background: style.bg, color: style.fg }}>
      {label}
    </Chip>
  )
}
