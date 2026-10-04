import { useId, useRef, type ReactNode, type RefObject } from 'react'
import { Monitor, Moon, Sparkles, Sun, X } from 'lucide-react'
import {
  useTheme,
  type Accent,
  type ChartStyle,
  type Density,
  type Theme,
} from '@/components/theme-provider'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { SegmentedControl, type SegmentedOption } from '@/components/ui/segmented-control'

// No colour values here: each swatch carries its accent's class, so it paints
// `--accent` exactly as that accent resolves in the current theme. Hard-coded
// dark tones used to preview colours the light theme never shows,
// and drifted from index.css whenever a hue moved.
const ACCENTS: { id: Accent; label: string }[] = [
  { id: 'teal', label: 'Teal' },
  { id: 'violet', label: 'Violet' },
  { id: 'lime', label: 'Lime' },
  { id: 'indigo', label: 'Indigo' },
  { id: 'magenta', label: 'Magenta' },
]

const THEME_OPTIONS: SegmentedOption<Theme>[] = [
  { value: 'system', label: <><Monitor className="size-3" aria-hidden="true" />System</> },
  { value: 'dark', label: <><Moon className="size-3" aria-hidden="true" />Dark</> },
  { value: 'light', label: <><Sun className="size-3" aria-hidden="true" />Light</> },
]

const DENSITY_OPTIONS: SegmentedOption<Density>[] = [
  { value: 'compact', label: 'Compact' },
  { value: 'cozy', label: 'Cozy' },
  { value: 'comfy', label: 'Comfy' },
]

const CHART_STYLE_OPTIONS: SegmentedOption<ChartStyle>[] = [
  { value: 'line', label: 'Line' },
  { value: 'line-only', label: 'Stroke' },
  { value: 'bar', label: 'Bars' },
]

/** Where the panel hangs when nothing anchored it: the sidebar footer corner. */
function fallbackAnchorRect(): DOMRect {
  const y = window.innerHeight - 12
  return { x: 12, y, left: 12, top: y, right: 12, bottom: y, width: 0, height: 0, toJSON: () => ({}) }
}

/**
 * Appearance settings: theme, accent, density, chart style.
 *
 * Opened from the sidebar's account controls. It used to be reached from a disc
 * fixed over the bottom-right of every page — over table rows, sticky form
 * actions and pagination on a phone, then from a panel
 * fixed to the bottom-left corner that covered the sidebar footer and the
 * page's cards like a stuck toast. It is now a popover hung from the
 * control that opened it: focus moves in, Escape or a press outside closes
 * it, and focus returns to that control.
 */
export function TweaksPanel({
  anchorRef,
  onClose,
}: {
  anchorRef: RefObject<HTMLElement | null>
  onClose: () => void
}) {
  const { theme, setTheme, accent, setAccent, density, setDensity, chartStyle, setChartStyle } =
    useTheme()
  const titleId = useId()
  // A detached anchor (the menu that opened the panel has closed) measures as
  // an empty rect at 0,0; fall back to the sidebar corner instead.
  const virtualRef = useRef<{ getBoundingClientRect: () => DOMRect }>({
    getBoundingClientRect: () => {
      const anchor = anchorRef.current
      return anchor?.isConnected
        ? anchor.getBoundingClientRect()
        : fallbackAnchorRect()
    },
  })

  return (
    <Popover open onOpenChange={(next) => { if (!next) onClose() }}>
      <PopoverAnchor virtualRef={virtualRef} />
      <PopoverContent
        aria-labelledby={titleId}
        side="top"
        align="start"
        sideOffset={8}
        collisionPadding={12}
        // The provider hands focus back to the anchor itself.
        onCloseAutoFocus={(event) => event.preventDefault()}
        // A press on the opener toggles it; letting it count as "outside"
        // would close the panel and reopen it on the same click.
        onInteractOutside={(event) => {
          if (anchorRef.current?.contains(event.target as Node)) event.preventDefault()
        }}
        // Non-modal, and it stays open while focus wanders (as before): the
        // account menu hands focus back to its trigger just after the panel
        // opens, which would otherwise close it at once.
        onFocusOutside={(event) => event.preventDefault()}
        className="w-[min(280px,calc(100vw-24px))] overflow-hidden p-0 shadow-lg bg-bg-elevated border-border-strong"
      >
        <div
          className="flex items-center gap-2 border-b px-3.5 py-2.5 border-border-subtle"
        >
          <Sparkles className="h-3 w-3 text-accent" aria-hidden="true" />
          <span id={titleId} className="text-body-sm font-semibold">
            Appearance
          </span>
          <div className="flex-1" />
          <button
            type="button"
            onClick={onClose}
            aria-label="Close appearance settings"
            className="rounded-sm p-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] text-fg-tertiary"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
        <div className="flex flex-col gap-3.5 p-3.5">
          <Group label="Theme">
            {/* "System" is a choice of its own: offering only the two resolved
                values overwrote it for good on the first click. */}
            <PanelSeg<Theme>
              label="Theme"
              value={theme}
              onChange={(v) => setTheme(v)}
              options={THEME_OPTIONS}
            />
          </Group>
          <Group label="Accent">
            <div role="group" aria-label="Accent" className="flex gap-1.5">
              {ACCENTS.map((a) => (
                <button
                  type="button"
                  key={a.id}
                  onClick={() => setAccent(a.id)}
                  title={a.label}
                  aria-label={a.label}
                  aria-pressed={accent === a.id}
                  className={`accent-${a.id} h-7 w-7 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]`}
                  style={{
                    background: 'var(--accent)',
                    border:
                      accent === a.id
                        ? '2px solid var(--fg)'
                        : '2px solid transparent',
                    outline: '1px solid var(--border)',
                  }}
                />
              ))}
            </div>
            {/* The swatches carry no text, so the choice is named under them. */}
            <div className="mt-1.5 text-caption text-fg-secondary" aria-hidden="true">
              {ACCENTS.find((a) => a.id === accent)?.label}
            </div>
          </Group>
          <Group label="Density">
            <PanelSeg<Density>
              label="Density"
              value={density}
              onChange={(v) => setDensity(v)}
              options={DENSITY_OPTIONS}
            />
          </Group>
          <Group label="Chart style">
            <PanelSeg<ChartStyle>
              label="Chart style"
              value={chartStyle}
              onChange={(v) => setChartStyle(v)}
              options={CHART_STYLE_OPTIONS}
            />
          </Group>
        </div>
      </PopoverContent>
    </Popover>
  )
}

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div
        className="micro-label mb-1.5 text-fg-tertiary"
      >
        {label}
      </div>
      {children}
    </div>
  )
}

/**
 * The shared SegmentedControl, stretched to the panel width with its
 * options sharing it equally. It replaces a hand-rolled copy of the same look.
 */
function PanelSeg<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: T
  onChange: (v: T) => void
  options: ReadonlyArray<SegmentedOption<T>>
}) {
  return (
    <SegmentedControl<T>
      aria-label={label}
      size="sm"
      value={value}
      onChange={onChange}
      options={options}
      className="flex w-full [&>button]:flex-1"
    />
  )
}
