import { cn } from '@/lib/utils'
import { initialsOf } from './initials'

/**
 * A person's initials on the identity chip colour.
 *
 * The ONE avatar: the app sidebar, the settings rail, Users, Profile and the
 * event-type owner list all render this. The background is always
 * `--avatar-bg`, whose lightness is pinned so white initials clear AA in both
 * themes (styles/tokens.css, theme-contrast.test.ts). Hashing a hue per person, or a
 * lighter hand-picked blue, broke that floor and showed one account in two
 * colours on the same screen.
 *
 * Decorative by default: every caller already prints the name beside it. Pass
 * `label` where the avatar stands alone and must be announced.
 */
export function UserAvatar({
  name,
  size = 26,
  label,
  className,
}: {
  /** Display name, falling back to the email; drives the initials. */
  name: string | null | undefined
  /** Diameter in px; the initials scale with it. */
  size?: number
  /** Accessible name when nothing next to the avatar says who it is. */
  label?: string
  className?: string
}) {
  return (
    <span
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      title={label}
      className={cn(
        'inline-flex shrink-0 select-none items-center justify-center rounded-full font-semibold leading-none text-white',
        className,
      )}
      style={{
        width: size,
        height: size,
        fontSize: Math.max(9, Math.round(size * 0.4)),
        background: 'var(--avatar-bg)',
      }}
    >
      {initialsOf(name)}
    </span>
  )
}
