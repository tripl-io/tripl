import { Info } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * An info icon that shows `help` on hover or focus, and names it for a screen
 * reader. A focusable button, not a `title` on a wrapper: a native tooltip
 * reaches neither the keyboard nor a touch screen, so the help that keeps
 * Coverage's and Reconciliation's numbers apart was mouse-only. Carries its own
 * provider, as TermHint does, so a page renders it without the app's root one
 * (page tests mount without it).
 *
 * For an explanation of a figure. A glossary term takes TermHint, which also
 * links to the Concepts page.
 */
export function InfoTip({ help }: { help: string }) {
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="inline-flex shrink-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            aria-label={help}
          >
            <Info className="size-3 text-fg-tertiary" aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs whitespace-normal">{help}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
