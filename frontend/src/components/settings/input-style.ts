import type { CSSProperties } from 'react'

/**
 * The form-control boundary: `--input`, pinned by measurement to the 3:1 WCAG
 * 1.4.11 floor for non-text contrast. `--border` is a hairline for row
 * separators (1.20-1.31:1) and left every kit field's edge close to invisible,
 * worst in dark mode (DS-8). The same token `ui/input` and `ui/switch` use, so
 * the two control families now share their edge (DS-9).
 */
export const INPUT_EDGE = '1px solid var(--input)'

/**
 * Corner radius shared with `ui/*` controls (`rounded-md` = --radius). The kit
 * used 7px against ui's 8px, one of the differences that made a settings row
 * and a dialog field look like two design systems (DS-9).
 */
export const INPUT_RADIUS = 'var(--radius)'

/**
 * The kit's control text size: 16px below `md`, the dense 12.5px (body-sm)
 * from `md` up (MT-27). iOS Safari zooms into any focused field under 16px,
 * so on a phone every tap on a 12.5px metric or fact-table field zoomed the
 * page. INPUT_BASE is an inline style, which cannot carry a media query, so
 * the breakpoint is a step function of the viewport width: `(768px - 100vw)`
 * is positive below `md` (clamped up to 16px) and zero or negative from it
 * (clamped down to body-sm). Same breakpoint as `md:` in INPUT_TEXT_CLASS.
 */
export const INPUT_FONT_SIZE = 'clamp(var(--text-body-sm), calc((768px - 100vw) * 1000), 16px)'

// Shared base style for kit text fields. Lives outside kit.tsx because that
// file must export only components (react-refresh/only-export-components);
// sibling controls (e.g. the column-suggest combobox) import it to render
// inputs that match the kit's text fields exactly.
//
// 32px / 12.5px from `md` up, the same as ui/input (DS-14): at 34px a kit
// field stood taller than the ui fields and the 32px buttons beside it.
export const INPUT_BASE: CSSProperties = {
  height: 32,
  width: '100%',
  borderRadius: INPUT_RADIUS,
  border: INPUT_EDGE,
  background: 'var(--bg)',
  color: 'var(--fg)',
  fontSize: INPUT_FONT_SIZE,
  padding: '0 10px',
}

/**
 * The same size for a control styled with classes rather than INPUT_BASE
 * (a hand-rolled select, a filter input): 16px on phones, body-sm from `md`.
 */
export const INPUT_TEXT_CLASS = 'text-base md:text-body-sm'

/**
 * One invalid look for every control (MT-7, AU-5): a `--danger` edge plus a
 * soft danger halo whenever the control carries `aria-invalid="true"`. The kit
 * sets `aria-invalid` on a row's control when its Field has an `error`, but
 * nothing styled it, so the only cue on a long form was 12px of red text.
 *
 * `!` because the kit's edge is an inline `border` (INPUT_BASE), which a plain
 * class cannot override. The `ui/input` family already maps `aria-invalid` to
 * `--destructive`, which is `--danger`.
 */
export const INPUT_INVALID_CLASS =
  'aria-invalid:border-(--danger)! aria-invalid:shadow-[0_0_0_3px_var(--danger-soft)]'

/**
 * Placeholder text in `--fg-faint` (MT-6 / DA-37), never the value colour, so
 * an example cannot pass for a filled-in field. Pair with an instruction-style
 * placeholder ("e.g. created_at"), see components/forms/placeholders.
 */
export const INPUT_PLACEHOLDER_CLASS = 'placeholder:text-(--fg-faint) placeholder:opacity-100'

/** Everything a kit text control needs on top of INPUT_BASE, as one className. */
export const INPUT_CLASS = `${INPUT_INVALID_CLASS} ${INPUT_PLACEHOLDER_CLASS}`

/**
 * Disabled treatment for every kit text field, textarea and select. Spread
 * after INPUT_BASE (it overrides the same `border` and `background` keys, so
 * no shorthand/longhand collision is introduced).
 *
 * Deliberately a shape change, not an opacity knock-down. The whole cue used
 * to be `opacity: 0.6`, which on the dark theme left a disabled input 3/255 of
 * fill and 7/255 of border away from a live one: Account · Security rendered
 * two dead password boxes indistinguishable from working ones, and the test
 * guarding "nothing here may look actionable" passed because it checked the
 * `disabled` DOM attribute rather than the appearance. Losing the
 * darker well and dashing the border reads at a glance at any contrast
 * setting, in either theme, and does not depend on telling two near-blacks
 * apart.
 */
export const INPUT_DISABLED: CSSProperties = {
  background: 'transparent',
  border: '1px dashed var(--border-strong)',
  color: 'var(--fg-subtle)',
  cursor: 'not-allowed',
}
