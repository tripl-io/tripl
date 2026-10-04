import { METRIC_COLOR_SWATCHES } from './metricDraft'

/**
 * The palette swatches plus a custom picker, shared by the metric and fact
 * table editors: the OS colour dialog was the only way to choose, and every
 * new metric or table got the same indigo. No single control for a
 * <label>, so the caller's row names the group; `inputId` is the custom
 * picker's id.
 */
export function ColorSwatches({
  value,
  onChange,
  inputId,
}: {
  value: string
  onChange: (color: string) => void
  inputId: string
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {METRIC_COLOR_SWATCHES.map(swatch => {
        const selected = value.toLowerCase() === swatch.value.toLowerCase()
        return (
          <button
            key={swatch.value}
            type="button"
            aria-label={swatch.label}
            aria-pressed={selected}
            title={swatch.label}
            onClick={() => onChange(swatch.value)}
            className="size-6 rounded-full border-2 transition-transform hover:scale-110"
            style={{
              background: swatch.value,
              borderColor: selected ? 'var(--fg)' : 'transparent',
              boxShadow: selected ? '0 0 0 2px var(--bg) inset' : undefined,
            }}
          />
        )
      })}
      <label
        className="ml-1 inline-flex cursor-pointer items-center gap-1.5 text-caption text-fg-secondary"
      >
        <input
          id={inputId}
          type="color"
          value={value}
          onChange={e => onChange(e.target.value)}
          className="h-6 w-8 cursor-pointer rounded-sm border bg-transparent border-border"
        />
        Custom…
      </label>
    </div>
  )
}
