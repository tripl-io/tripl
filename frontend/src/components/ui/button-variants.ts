import { cva } from "class-variance-authority"

/*
 * Disabled is a neutral surface, not the live colours at half opacity. A faded
 * primary still read as the brand call to action, just washed out, and its
 * text fell below legible contrast. Filled and outlined buttons turn
 * to the hover surface with --fg-muted text; the transparent ones (ghost,
 * danger, link) keep no fill and drop to --fg-faint. Both inks clear AA on
 * those surfaces (theme-contrast.test.ts).
 */
const DISABLED_FILLED =
  "disabled:border-[var(--border)] disabled:bg-[var(--surface-hover)] disabled:text-[var(--fg-muted)] disabled:shadow-none"
const DISABLED_BARE = "disabled:text-[var(--fg-faint)]"

/*
 * Button hierarchy. Pick by what the action does, not by how loud it
 * should feel:
 *   - default (solid accent, bg-accent-solid): the one primary create/save
 *     action per view
 *   - outline: secondary actions beside it
 *   - ghost: toolbar and inline actions
 *   - danger (bare red): destructive actions in rows, menus and detail pages
 *   - destructive (solid red): ONLY the confirm button inside a confirm dialog.
 *     A solid red navigation ("View signal") trains people to ignore it; use
 *     outline with a text-danger icon instead.
 *   - secondary, link: rare; prefer outline and a plain <Link>.
 *
 * Sizes match the app's 12.5px body, not the ui kit's 14px:
 *   - sm 28px: toolbars, filter rows, table actions. Fixed, not driven by
 *     density: FilterSelect chips, SegmentedControl sm and the FilterBar
 *     search field are 28px too, and a density-driven sm grew taller than
 *     `default` at comfy (DS-9 density lives on rows and panels instead)
 *   - default 32px: forms and dialogs
 *   - lg 36px: auth screens and empty-state calls to action
 *   - xs 24px: dense inline row actions only
 *   - icon 32px, icon-sm 28px, icon-xs 24px: icon-only (use IconButton)
 * Every size shares the 7px control radius, and icons default to 16px, or
 * 14px on sm/xs/icon-xs.
 */

// Per size rather than in the base, so a size's icon default never depends on
// which of two same-variant utilities Tailwind happens to emit last.
const ICON_16 = "[&_svg:not([class*='size-'])]:size-4"
const ICON_14 = "[&_svg:not([class*='size-'])]:size-3.5"

export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-control text-body-sm font-medium transition-all cursor-pointer disabled:pointer-events-none [&_svg]:pointer-events-none shrink-0 [&_svg]:shrink-0 outline-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
  {
    variants: {
      variant: {
        default:
          `bg-accent-solid text-accent-solid-fg shadow-xs hover:bg-accent-solid/90 ${DISABLED_FILLED}`,
        destructive:
          `bg-destructive text-destructive-foreground shadow-xs hover:bg-destructive/90 focus-visible:ring-destructive/20 ${DISABLED_FILLED}`,
        outline:
          `border border-input bg-background shadow-xs hover:bg-surface-active hover:text-foreground active:bg-surface-active active:text-foreground ${DISABLED_FILLED}`,
        secondary:
          `bg-secondary text-secondary-foreground shadow-xs hover:bg-secondary/80 ${DISABLED_FILLED}`,
        ghost:
          `hover:bg-surface-hover hover:text-foreground active:bg-surface-active active:text-foreground ${DISABLED_BARE}`,
        danger:
          `text-[var(--danger)] hover:bg-[var(--danger-soft)] hover:text-[var(--danger)] active:bg-[var(--danger-soft)] active:text-[var(--danger)] ${DISABLED_BARE}`,
        link: `text-primary underline-offset-4 hover:underline ${DISABLED_BARE}`,
      },
      size: {
        default: `h-8 px-3 has-[>svg]:px-2.5 ${ICON_16}`,
        xs: `h-6 gap-1 px-2 has-[>svg]:px-1.5 text-caption ${ICON_14}`,
        sm: `h-7 gap-1.5 px-2.5 has-[>svg]:px-2 ${ICON_14}`,
        lg: `h-9 px-4 has-[>svg]:px-3.5 text-body ${ICON_16}`,
        icon: `size-8 ${ICON_16}`,
        "icon-sm": `size-7 ${ICON_14}`,
        "icon-xs": `size-6 ${ICON_14}`,
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)
