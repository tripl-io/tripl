import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

/**
 * A code value or identifier shown as a token: `ios`, `${platform}`,
 * `prod_monthly`, an enum value. Square-ish (`rounded-sm`), sunken, mono
 * at regular weight — it is data, not a status, so it is never a pill. Replaces
 * the hand-rolled `rounded border px-1.5 py-0.5 text-[10px]` pills.
 */
export function CodeToken({ className, ...props }: ComponentProps<"code">) {
  return (
    <code
      data-slot="code-token"
      className={cn(
        "mono inline-block h-[18px] max-w-full shrink-0 truncate rounded-sm border border-border-subtle bg-bg-sunken px-1.5 align-middle text-micro font-normal leading-4 text-fg",
        className,
      )}
      {...props}
    />
  )
}
