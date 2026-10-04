import { cn } from "@/lib/utils"

export type DotTone = "neutral" | "success" | "warning" | "danger" | "info" | "accent"

const COLORS: Record<DotTone, string> = {
  neutral: "var(--fg-faint)",
  success: "var(--success)",
  warning: "var(--warning)",
  danger: "var(--danger)",
  info: "var(--info)",
  accent: "var(--accent)",
}

type DotProps = {
  tone?: DotTone
  pulse?: boolean
  size?: number
  className?: string
  /**
   * What the colour means, for a dot that stands alone. Several statuses share
   * a tone, and colour is never enough on its own (WCAG 1.4.1), so a dot with
   * no visible label beside it names itself to screen readers and on hover.
   * Leave it off when the meaning is already written next to it.
   */
  label?: string
}

export function Dot({ tone = "neutral", pulse = false, size = 7, className, label }: DotProps) {
  const dot = (
    <span
      aria-hidden="true"
      title={label}
      className={cn("inline-block rounded-full shrink-0", pulse && "pulse-dot", className)}
      style={{ width: size, height: size, background: COLORS[tone] }}
    />
  )
  if (!label) return dot
  return (
    <>
      {dot}
      <span className="sr-only">{label}</span>
    </>
  )
}
